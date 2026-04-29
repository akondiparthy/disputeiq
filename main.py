import asyncio
import json
import os
import re
from typing import AsyncGenerator

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from data import CASES, MERCHANT_DB, REASON_CODE_DB, TRANSACTION_DB

app = FastAPI(title="DisputeIQ API", version="1.0.0", description="Agentic chargeback analysis service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten to your frontend domain in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TOOLS = [
    {
        "name": "lookup_transaction",
        "description": (
            "Look up a full transaction record by ID. Returns authorization data, 3DS results, "
            "device fingerprint, AVS/CVV results, shipping info, and any post-delivery activity."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "transaction_id": {"type": "string", "description": "The transaction ID to retrieve"}
            },
            "required": ["transaction_id"],
        },
    },
    {
        "name": "get_reason_code_details",
        "description": (
            "Get detailed requirements for a Visa or Mastercard chargeback reason code, "
            "including required evidence, fight/accept indicators, time limits, and win rate benchmark."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason_code": {"type": "string", "description": "The reason code (e.g., '10.4', '4863')"},
                "network": {"type": "string", "enum": ["visa", "mastercard"]},
            },
            "required": ["reason_code", "network"],
        },
    },
    {
        "name": "get_merchant_dispute_history",
        "description": "Get a merchant's historical dispute win rate, dispute ratio, and patterns.",
        "input_schema": {
            "type": "object",
            "properties": {
                "merchant_id": {"type": "string", "description": "The merchant ID"}
            },
            "required": ["merchant_id"],
        },
    },
]

SYSTEM_PROMPT = """You are DisputeIQ, an expert fraud operations analyst specializing in chargeback representment decisions for fintech merchants operating in emerging markets (LatAm, Africa, Southeast Asia).

Your role: analyze chargeback claims systematically using available tools, then deliver a precise recommendation.

Mandatory workflow:
1. Call lookup_transaction with the transaction ID to retrieve all evidence
2. Call get_reason_code_details with the reason code and network to understand requirements
3. Call get_merchant_dispute_history with the merchant ID to assess historical context
4. After all three tools have been called, produce your final JSON analysis

Special attention: When reviewing MC 4863 cases, always check for friendly fraud indicators — late filing delay (20+ days post-delivery), product activation by cardholder, login activity post-delivery, device fingerprint match to known cardholder profile.

Final output: After all tool calls are complete, respond with ONLY a valid JSON object. No markdown, no backticks, no preamble, no explanation — raw JSON only.

Required JSON schema:
{
  "recommendation": "FIGHT" or "ACCEPT",
  "confidence": 0.0-1.0,
  "reason_code_classification": {
    "code": "10.4",
    "network": "visa",
    "name": "Other Fraud – Card Absent Environment"
  },
  "evidence_strength": "STRONG" or "MODERATE" or "WEAK",
  "key_evidence_for_fight": ["point 1", "point 2", "point 3"],
  "key_evidence_against": ["risk 1", "risk 2"],
  "estimated_win_probability": 0.0-1.0,
  "friendly_fraud_flag": true or false,
  "representment_letter": "Full formal representment letter addressed to the card issuing bank. Must cite specific transaction evidence: auth codes, 3DS ECI codes, device fingerprint history, delivery confirmation, activation records. Reference the applicable Mastercard or Visa rule. Empty string if recommending ACCEPT.",
  "summary": "2-3 sentence plain-English explanation of the decision rationale."
}"""


def execute_tool(name: str, input_data: dict) -> dict:
    if name == "lookup_transaction":
        txn_id = input_data.get("transaction_id", "")
        result = TRANSACTION_DB.get(txn_id)
        return result if result else {"error": f"Transaction {txn_id} not found"}

    if name == "get_reason_code_details":
        network = input_data.get("network", "")
        code = input_data.get("reason_code", "")
        key = f"{network}_{code}"
        result = REASON_CODE_DB.get(key)
        return result if result else {"error": f"Reason code {network}/{code} not found"}

    if name == "get_merchant_dispute_history":
        merchant_id = input_data.get("merchant_id", "")
        merchant = MERCHANT_DB.get(merchant_id)
        if not merchant:
            return {"error": f"Merchant {merchant_id} not found"}
        return {
            **merchant,
            "win_rate": round(merchant["won"] / merchant["total_disputes"], 3),
            "loss_rate": round(merchant["lost"] / merchant["total_disputes"], 3),
        }

    return {"error": f"Unknown tool: {name}"}


def content_block_to_dict(block) -> dict:
    """Convert anthropic SDK content block objects to plain dicts for message history."""
    if block.type == "text":
        return {"type": "text", "text": block.text}
    elif block.type == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return {"type": block.type}


async def run_agent_stream(case: dict) -> AsyncGenerator[str, None]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        yield f"data: {json.dumps({'type': 'error', 'message': 'ANTHROPIC_API_KEY not configured'})}\n\n"
        return

    aclient = anthropic.AsyncAnthropic(api_key=api_key)

    initial_message = (
        f"Analyze this chargeback claim:\n\n"
        f"Claim ID: {case['id']}\n"
        f"Transaction ID: {case['transaction_id']}\n"
        f"Merchant: {case['merchant']} (Merchant ID: {case['merchant_id']})\n"
        f"Cardholder: {case['cardholder']}\n"
        f"Transaction Amount: ${case['amount']:.2f}\n"
        f"Card Network: {case['network'].upper()}\n"
        f"Reason Code: {case['reason_code']}\n"
        f"Days Since Transaction: {case['days_since_txn']}\n"
        f"Filing Date: {case['filing_date']}\n"
        f"Cardholder Claim: \"{case['claim']}\"\n\n"
        f"Use your tools to investigate, then produce the final JSON recommendation."
    )

    messages = [{"role": "user", "content": initial_message}]

    try:
        for _turn in range(8):
            response = await aclient.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4000,
                system=SYSTEM_PROMPT,
                tools=TOOLS,  # type: ignore[arg-type]
                messages=messages,  # type: ignore[arg-type]
            )

            # Add assistant response to history (convert SDK objects to dicts)
            messages.append({
                "role": "assistant",
                "content": [content_block_to_dict(b) for b in response.content],
            })

            tool_blocks = [b for b in response.content if b.type == "tool_use"]
            text_blocks = [b for b in response.content if b.type == "text"]

            # Stream any thinking/reasoning text
            for tb in text_blocks:
                text = tb.text.strip()
                if text:
                    yield f"data: {json.dumps({'type': 'thinking', 'content': text})}\n\n"
                    await asyncio.sleep(0)  # Yield control to allow flush

            # If no tool calls, this is the final response
            if not tool_blocks:
                raw = " ".join(b.text for b in text_blocks)
                match = re.search(r"\{[\s\S]*\}", raw)
                if match:
                    try:
                        verdict = json.loads(match.group())
                        yield f"data: {json.dumps({'type': 'verdict', 'data': verdict})}\n\n"
                    except json.JSONDecodeError as e:
                        yield f"data: {json.dumps({'type': 'error', 'message': f'Failed to parse verdict JSON: {e}'})}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'error', 'message': 'Agent did not return structured JSON', 'raw': raw[:300]})}\n\n"
                break

            # Execute each tool call and stream the result
            tool_results = []
            for block in tool_blocks:
                result = execute_tool(block.name, block.input)
                yield f"data: {json.dumps({'type': 'tool_call', 'tool': block.name, 'input': block.input, 'result': result})}\n\n"
                await asyncio.sleep(0)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

            messages.append({"role": "user", "content": tool_results})

    except anthropic.AuthenticationError:
        yield f"data: {json.dumps({'type': 'error', 'message': 'Invalid ANTHROPIC_API_KEY'})}\n\n"
    except anthropic.RateLimitError:
        yield f"data: {json.dumps({'type': 'error', 'message': 'Rate limit reached. Please retry in a moment.'})}\n\n"
    except anthropic.APIError as e:
        yield f"data: {json.dumps({'type': 'error', 'message': f'Anthropic API error: {str(e)}'})}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'message': f'Unexpected error: {str(e)}'})}\n\n"
    finally:
        yield f"data: {json.dumps({'type': 'done'})}\n\n"


# ─────────────────────────── Routes ────────────────────────────

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "DisputeIQ API", "version": "1.0.0"}


@app.get("/api/cases")
def list_cases():
    return {"cases": CASES, "total": len(CASES)}


@app.get("/api/cases/{case_id}")
def get_case(case_id: str):
    case = next((c for c in CASES if c["id"] == case_id), None)
    if not case:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")
    return case


@app.post("/api/analyze/{case_id}")
async def analyze_case(case_id: str):
    case = next((c for c in CASES if c["id"] == case_id), None)
    if not case:
        raise HTTPException(status_code=404, detail=f"Case {case_id} not found")

    return StreamingResponse(
        run_agent_stream(case),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
