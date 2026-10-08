# AWS Solana Smart Money Radar

Clean deployment copy for the paper-trading radar. It contains source, migrations,
tests, and environment templates only. It intentionally excludes the existing
local database, API credentials, Python virtual environment, and Node build files.
The first start creates a new database, so wallet history and paper results begin
fresh on this deployment.

## What is included

- FastAPI backend, SQLAlchemy models, Alembic migrations, and paper-trading worker.
- Helius-first Solana ingestion with Alchemy fallback for tracked-wallet history
  and standard Solana RPC safety checks.
- Next.js dashboard source (optional on AWS if you only need the bot/Telegram).
- Tests for cluster detection, wallet score, paper rules, and other logic.

The bot is paper-only. This bundle does not sign or submit live trades.

## Provider behavior

Set either `HELIUS_API_KEY`, `ALCHEMY_API_KEY`, or both in `backend/.env`.
Helius is preferred when both are present. If its request fails, the backend tries
Alchemy; if only Alchemy is configured, Alchemy is used directly. The ingestion
status endpoint reports configured providers and the last active provider.

Helius Enhanced returns parsed transactions. Alchemy's
[`getTransactionsForAddress`](https://www.alchemy.com/docs/chains/solana/solana-api-endpoints/get-transactions-for-address)
returns full Solana transaction metadata; the backend normalizes wallet-owned token
balance deltas for cluster detection. The endpoint returns at most 100 full
transactions per request. This worker checks the latest page each polling cycle;
it is intended for ongoing tracking, not a full historical backfill. Both providers
have plan-specific quotas and rate limits, so fallback works only while the
alternate provider has available quota and access. The safety endpoints use
standard Solana RPC methods supported by [Alchemy](https://www.alchemy.com/docs/solana/solana-api-overview).

## Upload from Windows to AWS

Upload the whole `aws_sniper_bot` directory using SFTP or SCP. Do not upload local
`.env` files, `radar.db`, `.venv`, `node_modules`, or `.next`. They are not needed.

For example, from PowerShell (replace the host and key-file path):

```powershell
scp -i C:\path\to\aws-key.pem -r .\aws_sniper_bot ubuntu@YOUR_EC2_PUBLIC_IP:/home/ubuntu/
```

For an Ubuntu EC2 instance, connect and install runtime packages:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
cd /home/ubuntu/aws_sniper_bot/backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
nano .env
chmod 600 .env
```

In `.env`, add API keys and optional Telegram values. Keep `DATA_MODE=live` and
leave `DATABASE_URL=sqlite:///./radar.db` for the simplest single-instance setup.
Because this is a fresh folder and no database was uploaded, this creates clean
tables and paper account state. Then migrate and start once to check it:

```bash
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In another SSH session, verify `curl http://127.0.0.1:8000/api/health` and
`curl http://127.0.0.1:8000/api/ingestion/status`. Stop the foreground server with
Ctrl+C before enabling the service below.

### Run continuously with systemd

Copy the included service file and enable it:

```bash
sudo cp /home/ubuntu/aws_sniper_bot/deploy/smart-money-radar.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now smart-money-radar
sudo systemctl status smart-money-radar
sudo journalctl -u smart-money-radar -f
```

The service listens on `127.0.0.1:8000`, so it is not directly exposed to the
internet. If you later host the dashboard publicly, put it behind HTTPS and add
authentication/access controls before exposing trading controls or account data.

### Optional dashboard

The dashboard needs Node.js 20 or newer. Install that runtime using your preferred
Node version manager, then run:

On a machine where you want to serve the UI:

```bash
cd /home/ubuntu/aws_sniper_bot/frontend
cp .env.example .env.local
nano .env.local  # set NEXT_PUBLIC_API_URL to the backend URL reachable by browsers
npm ci
npm run build
npm start
```

For a public UI, use a reverse proxy with HTTPS and configure backend
`CORS_ORIGINS` to the exact dashboard origin. Do not open port 8000 to the public
internet without an authentication layer.

## Fresh local setup

### Backend (PowerShell)

```powershell
cd aws_sniper_bot\backend
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env and add one or both provider keys
alembic upgrade head
uvicorn app.main:app --reload
```

### Frontend (second terminal)

```powershell
cd aws_sniper_bot\frontend
npm ci
Copy-Item .env.example .env.local
npm run dev
```

Open `http://localhost:3000`. For sample/mock data, set `DATA_MODE=mock` in
`backend/.env`; restart the backend after changing environment values.

## Database and fresh data

The default is a local SQLite file at `backend/radar.db`, created when migrations
run. That file is runtime data and is ignored by Git. Keep a private backup before
replacing an existing database. To intentionally reset a deployment, stop the
service, back up and remove only `backend/radar.db`, then run `alembic upgrade head`
and restart. Do not remove a production database unless you intend to discard its
trade history and paper positions.

PostgreSQL is also supported through `DATABASE_URL`, for example
`postgresql+psycopg://radar:password@127.0.0.1:5432/radar`, followed by the same
Alembic migration. SQLite is simpler for a single low-volume bot process.

## Tests

From `backend/` with the virtual environment active:

```bash
python -m pytest
```

Never commit `.env`, API keys, Telegram bot tokens, wallet signing keys, or a live
database. This project should remain in paper mode until its data and execution
behavior have been independently reviewed.
