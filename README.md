# DisputeIQ — Agentic Chargeback Copilot

A multi-step AI agent that analyzes chargeback claims, classifies Visa/Mastercard reason codes, evaluates transaction evidence, and generates formal representment letters — replacing a 45-minute analyst workflow with a 30-second automated decision.

## What It Does

1. **Ingests** a chargeback claim (cardholder, amount, reason code, claim text)
2. **Runs a 3-tool agentic loop** via Claude's tool use API:
   - `lookup_transaction` — retrieves 3DS, AVS/CVV, device fingerprint, delivery status
   - `get_reason_code_details` — returns evidence requirements and fight/accept indicators
   - `get_merchant_dispute_history` — surfaces win rate context and dispute patterns
3. **Produces a structured verdict** — FIGHT or ACCEPT with confidence, evidence summary, win probability
4. **Generates a representment letter** ready to submit to the issuing bank
5. **Flags friendly fraud** (first-party misuse) on MC 4863 cases using behavioral signals

## Cases Included

| Case | Reason Code | Expected | Why |
|------|------------|----------|-----|
| CB-2025-001 | Visa 10.4 | FIGHT | 3DS ECI-05, liability shifts to issuer |
| CB-2025-002 | MC 4841 | FIGHT | No cancellation on file, service used post-claim |
| CB-2025-003 | Visa 13.1 | ACCEPT | Label created only, no carrier scan, 45 days |
| CB-2025-004 | MC 4853 | FIGHT | SKU match, signed agreement, IMEI registered |
| CB-2025-005 | MC 4863 | FIGHT + Friendly Fraud flag | 3DS ECI-02, product activated by cardholder, signed delivery |

## Architecture

```
disputeiq/
├── DisputeIQ.jsx         # React frontend (works standalone or with backend)
└── backend/
    ├── main.py           # FastAPI app with SSE streaming
    ├── data.py           # Transaction, reason code, merchant databases
    ├── requirements.txt
    ├── Dockerfile
    └── .env.example
```

**Two runtime modes:**
- **Browser mode** (default): The React component calls the Anthropic API directly. Set `BACKEND_URL = ""` in `DisputeIQ.jsx`. Good for demos.
- **Backend mode**: The React component calls your FastAPI server, which runs the agent server-side and streams events via SSE. Set `BACKEND_URL = "https://your-deployment.railway.app"`.

## Local Development

### Backend

```bash
cd backend
cp .env.example .env          # Add your ANTHROPIC_API_KEY
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

API will be live at `http://localhost:8000`. Verify with:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/api/cases
curl -X POST http://localhost:8000/api/analyze/CB-2025-001
```

### Frontend

Set in `DisputeIQ.jsx`:
```javascript
const BACKEND_URL = "http://localhost:8000";
```

Then open the JSX in Claude.ai artifacts, or drop it into a Vite/CRA project.

## Deploy to Railway (Recommended)

1. Push the `backend/` folder to a GitHub repo
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Set environment variable: `ANTHROPIC_API_KEY=sk-ant-...`
4. Railway auto-detects the Dockerfile and deploys
5. Copy the Railway URL (e.g. `https://disputeiq-production.up.railway.app`)
6. Set `BACKEND_URL` in `DisputeIQ.jsx` to that URL

## Deploy to Render

1. New Web Service → connect your repo
2. Runtime: Docker
3. Environment variable: `ANTHROPIC_API_KEY`
4. Deploy

## API Reference

```
GET  /health                          → service status
GET  /api/cases                       → list all 5 cases
GET  /api/cases/{case_id}             → single case detail
POST /api/analyze/{case_id}           → SSE stream of agent events
```

### SSE Event Types

```json
{ "type": "thinking",  "content": "Analyzing 3DS authentication data..." }
{ "type": "tool_call", "tool": "lookup_transaction", "input": {...}, "result": {...} }
{ "type": "verdict",   "data": { "recommendation": "FIGHT", "confidence": 0.91, ... } }
{ "type": "error",     "message": "..." }
{ "type": "done" }
```

## Key Technical Decisions

- **No LangChain / no framework** — raw multi-turn tool use loop against the Anthropic Messages API. Each turn sends full conversation history, handles tool_use blocks, executes tools, sends tool_results. Pure signal on how Claude's tool use actually works.
- **SSE over WebSocket** — agentic workflows are one-way streams (agent → client). SSE is simpler, works over HTTP/2, no handshake overhead.
- **asyncio.sleep(0)** after each yield in the FastAPI generator — releases the event loop so FastAPI flushes the SSE buffer immediately after each tool call. Without this, all events arrive in one batch at the end.
- **Structured JSON contract** — system prompt specifies exact schema. Claude returns raw JSON as the final message (no markdown, no backticks). Frontend uses regex to extract the JSON block defensively.

## Portfolio Framing

When writing about this project:

> "Built a production-grade agentic system that reduces chargeback analyst review time from ~45 minutes to under 60 seconds. The agent executes a deterministic 3-tool investigation loop — transaction lookup, reason code evaluation, merchant history — then synthesizes evidence into a structured FIGHT/ACCEPT decision with a formal representment letter. Implemented a first-party misuse (friendly fraud) detection layer on MC 4863 cases using behavioral signals: delivery signature, product activation timestamp, device fingerprint continuity, and chargeback filing lag."

Stack keywords for your resume/LinkedIn: `agentic AI · multi-turn tool use · FastAPI · SSE streaming · Anthropic Claude · chargeback representment · Visa/Mastercard dispute rules · friendly fraud detection`
