# GhostReg

A web-based control panel for automated JetBrains account registration.

## Overview

GhostReg provides two registration modes:
- **Semi-automatic (browser)**: Uses DrissionPage to control Chrome/Edge/Brave; user solves CAPTCHAs manually or via AI
- **Protocol (pure HTTP)**: Uses httpx/curl_cffi for bulk headless registration

Email generation is done via the YYDS Mail API.

## Tech Stack

- **Language**: Python 3.12
- **Backend**: FastAPI + Uvicorn (ASGI)
- **Browser Automation**: DrissionPage
- **HTTP Clients**: httpx, curl_cffi, requests
- **Frontend**: Static HTML/JS/CSS + WebSockets (real-time updates)
- **Proxy**: Cloudflare Workers script (`workers/ghostreg_proxy.js`)

## Project Structure

```
ghostreg/           # Main module (browser automation + web UI)
  main.py           # Entry point / CLI
  server.py         # FastAPI app + WebSocket endpoints
  register.py       # Core browser automation logic
  email_service.py  # YYDS Mail API integration
  captcha_service.py / captcha_solver.py  # CAPTCHA handling
  config.py         # Configuration + persistence
  static/           # Frontend assets (index.html)
protocol_reg/       # Pure HTTP registration module
  jb_api.py         # JetBrains API calls
  worker.py         # Bulk registration orchestration
workers/            # Cloudflare Workers proxy script
output/             # Auto-generated: accounts.json, accounts.csv
```

## Running

```bash
python -m ghostreg              # Start on port 5000 (default)
python -m ghostreg --port 8080  # Custom port
```

## Configuration

- YYDS Mail API key: set via the web UI Settings tab (stored in `output/settings.json`)
- CAPTCHA platform (YesCaptcha / CapSolver): configured via the web UI

## Ports

- Web UI: **5000** (0.0.0.0)
