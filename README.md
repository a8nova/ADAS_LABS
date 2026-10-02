# ADAS Labs — Carrier Route

Street-video dispatch for collection boxes. Indexed captions decide who **used** a mailbox versus who **walked past**. Drop-offs stay on the loop; walk-bys stay off it.

Built on the VAST Video Search and Summarization (VSS) archive during the Builders Challenge. Live demo (team-46): [http://video-lab-team-46.cosmos.vastdata.com/app](http://video-lab-team-46.cosmos.vastdata.com/app)

## What it does

1. **Shift** — name the depot and pick a neighborhood (San Francisco streets by default).
2. **Evidence** — Hit vs Close from captions, with the clip and the sentence that classified it.
3. **Route** — schematic map of boxes to hit, walk-bys to skip, and the loop back to the office.

Classification is caption-based (Explore / VastDB), not hybrid search, so it does not depend on Cosmos-Embed1 being up.

- **Serve / Hit** — backpack + “using a blue mailbox on the sidewalk”
- **Pass / Close** — someone walked past the mailbox and did not drop mail

## Apps

| Path | Role |
| --- | --- |
| `carrier-route/` | Demo board (Shift / Evidence / Route). This is what is deployed at `/app`. |
| `mailbox-watch/` | Earlier mailbox finder used while we were wiring search and clips. |

## Run locally against a VSS backend

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r carrier-route/requirements.txt

export VSS_URL="https://your-vss-host"
export VSS_USERNAME="your-user"
export VSS_PASSWORD="your-password"
python3 carrier-route/main.py
```

Open `http://127.0.0.1:8080`. Do not commit credentials.

The HTTP server serves `/` and proxies:

- `GET /api/route?area=san_francisco&start=Main%20office&end=Main%20office`
- `GET /api/clip?source=s3://...`

When the app sits behind Ingress at `/app`, the UI calls `/app/api/...` (the rewrite strips `/app` before the process).

## Deploy without a registry

Use a public `python:3.12-slim` image, mount this code from a ConfigMap, and put VSS credentials in a Secret. Expose path `/app` on the existing team host.

```bash
kubectl -n "$NS" create configmap mailbox-watch-code \
  --from-file=carrier-route \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$NS" rollout restart deploy/mailbox-watch
```

## Environment

| Variable | Purpose |
| --- | --- |
| `VSS_URL` | Retrieval backend base URL (in-cluster: `http://video-backend-service:8000`) |
| `VSS_USERNAME` / `VSS_PASSWORD` | VSS login for JWT |
| `PORT` | HTTP port (default `8080`) |
