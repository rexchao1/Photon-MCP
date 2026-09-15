# Photon Commerce MCP server

[![PyPI](https://img.shields.io/pypi/v/photon-mcp.svg)](https://pypi.org/project/photon-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/photon-mcp.svg)](https://pypi.org/project/photon-mcp/)
[![CI](https://github.com/Photon-Commerce/Photon-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/Photon-Commerce/Photon-MCP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Turn invoices, receipts, checks, remittances, bank statements, utility bills, pay stubs,
shipping labels and bills of lading into structured JSON — from inside Claude or any other
MCP client.

This is an [MCP](https://modelcontextprotocol.io) server for the
[Photon Commerce API](https://apidocs.photoncommerce.com/). It runs locally over stdio and
talks to nothing but the Photon REST API — no database, no cloud storage, no hosting to
stand up. You bring your own API credentials and it acts on your behalf.

## Quickstart

This Quickstart command is for **Claude Code (CLI)**. Using Claude Desktop instead? Skip
to [Configure your client](#configure-your-client) 

You need Python 3.10 or newer and a set of Photon API credentials
([get them below](#get-credentials)).

```bash
claude mcp add photon -e PHOTON_CLIENT_ID=... \
  -e PHOTON_SECRET_KEY=... \
  -e PHOTON_USERNAME=you@example.com \
  -e PHOTON_API_KEY=... \
  -e PHOTON_PASSWORD=... \
  -e PHOTON_ALLOWED_DIRS=/Users/you/Documents/invoices \
  -- uvx photon-mcp
```

Then just ask for what you want — *"pull the totals out of
~/Documents/invoices/acme.pdf"* or *"what kind of document is this?"*

## Get credentials

If you don't already have an account, register for a free sandbox one:

```bash
curl -X POST https://sandbox-api.photoncommerce.com/api/v4/register \
  -F first_name=Jane -F last_name=Doe \
  -F email=you@example.com -F password='YourPassw0rd!'
```

You get back a `client_id`, `api_key`, `secret_key` and `username`. Those plus your
password are the five values the server needs. Sandbox accounts include 20 documents over
15 days. The first 3 process straight away; to process the remaining 17, verify your
account with Photon Commerce.

For a production account, email <api@photoncommerce.com>.

## Configure your client

### Claude Desktop

In `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "photon": {
      "command": "uvx",
      "args": ["photon-mcp"],
      "env": {
        "PHOTON_CLIENT_ID": "...",
        "PHOTON_SECRET_KEY": "...",
        "PHOTON_USERNAME": "you@example.com",
        "PHOTON_API_KEY": "...",
        "PHOTON_PASSWORD": "...",
        "PHOTON_ENV": "sandbox",
        "PHOTON_ALLOWED_DIRS": "/Users/you/Documents/invoices"
      }
    }
  }
}
```

### Other MCP clients

Any client that speaks stdio works. Run `uvx photon-mcp` (or `photon-mcp` if you installed
it with pip) as the command, pass no arguments, and supply the credentials as environment
variables.

## Tools

| Tool | What it does | Arguments |
|---|---|---|
| `process_document` | Extract structured fields from a document | `file_path` **or** `url`; optional `doctype`, `page_start`, `page_end`, `subaccount`, `reference_id` |
| `get_extraction` | Fetch a stored extraction back by key | `photon_key` |
| `classify_document` | Detect a document's type and suggest a `doctype` | `file_path` **or** `url` |
| `split_document` | Find page boundaries in a PDF holding several documents | `file_path` **or** `url` |
| `correct_fields` | Amend header fields on a stored extraction | `photon_key`, `fields` |
| `add_line_item` | Add a line item to a stored extraction | `photon_key`, `fields` |
| `correct_line_item` | Amend a single line item | `photon_key`, `line_item_id`, `fields` |
| `delete_line_item` | Delete a line item — **irreversible** | `photon_key`, `line_item_id` |
| `save_original_document` | Download the source file to a local folder | `doc_path`, `directory` |
| `get_usage` | Report API calls and pages used | optional `year`, `month`, `per_subaccount` |
| `check_connection` | Verify credentials and API health | none |
| `list_document_types` | List doctypes, file formats and size limits; makes no API call | none |

Every processed document gets a `photon_key` — that is the handle for fetching, amending
and downloading it later.

`delete_line_item` is the only tool marked `destructive_hint`, so a well-behaved client
will confirm before calling it. `get_extraction`, `get_usage`, `check_connection` and
`list_document_types` are marked read-only.

The four editing tools (`correct_fields`, `add_line_item`, `correct_line_item`,
`delete_line_item`) are enabled per account. If they return an error, email
<support@photoncommerce.com> to have editing turned on.

## Environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `PHOTON_CLIENT_ID` | Yes | — | From registration |
| `PHOTON_SECRET_KEY` | Yes | — | From registration |
| `PHOTON_USERNAME` | Yes | — | Your registered email |
| `PHOTON_API_KEY` | Yes | — | From registration |
| `PHOTON_PASSWORD` | Yes | — | Your account password |
| `PHOTON_ENV` | No | `sandbox` | Set to `production` for a live account |
| `PHOTON_BASE_URL` | No | — | Overrides `PHOTON_ENV` entirely |
| `PHOTON_ALLOWED_DIRS` | No | unrestricted | `:`-separated on macOS/Linux, `;` on Windows; see [Security](#security) |
| `PHOTON_TIMEOUT_SECONDS` | No | `180` | Per-request HTTP timeout |
| `PHOTON_LOG_LEVEL` | No | `WARNING` | Logs go to stderr, never stdout |

## Security

**Set `PHOTON_ALLOWED_DIRS`.** It is a list of directories the server may read documents
from and write downloads to, separated the same way `PATH` is on your platform — `:` on
macOS and Linux, `;` on Windows. Leave it unset and the server will upload whatever path
it is handed; set it and anything outside those folders is refused. Since the thing
choosing the paths is a language model, it is a sensible boundary to draw.

Credentials are read from the environment only — the server never writes them to disk and
never logs them. Keep them in your MCP client's config rather than in a shell profile, and
use a sandbox account for anything exploratory.

## Document types and limits

Pass one of `invoice`, `receipt`, `check`, `stub`, `remittance`, `statement`,
`bill-utility`, `bol`, `shippinglabel` or `invoice-commercial` as `doctype`. Leave it off
and the document is treated as an invoice. `classify_document` reports types from the same
set, so its `suggested_doctype` can go straight into `process_document`.

Files must be between 1 KB and 50 MB. PDF, PNG, JPEG, TIFF, HEIC, DOC/DOCX, XLS/XLSX, HTML
and TXT are accepted. The API allows 10 requests per second.

## Results

Every tool returns `{"ok": true, ...}` or `{"ok": false, "error": "..."}`, with a `hint`
attached when there is something specific you can do about the failure. The bulky
`Raw_Text` field is stripped from extractions before they reach the model, since it eats
context and is rarely what you want.

Depending on how your account is configured, `process_document` either returns the
extraction immediately or queues the document. It handles both — check `status`:

- `extracted` — the data is in `extraction`
- `queued` — call `get_extraction` with the `photon_key` shortly

Asynchronous processing is enabled per account by <sales@photoncommerce.com>, with a
standard 24-hour turnaround and faster options available.

## From source

```bash
git clone https://github.com/Photon-Commerce/Photon-MCP
cd Photon-MCP
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Point your client at the absolute path of the entry point,
`/path/to/Photon-MCP/.venv/bin/photon-mcp`, instead of `uvx photon-mcp`.

### Development

```bash
pytest        # 66 tests, fully offline against a stubbed transport
ruff check .
```

No credentials or network access are needed to run the suite. To exercise the server by
hand, copy `.env.example` to `.env`, fill it in, and drive it over stdio:

```bash
set -a && . ./.env && set +a
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"cli","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"check_connection","arguments":{}}}' \
  | photon-mcp
```

The server speaks JSON-RPC on stdout and nothing else — all logging goes to stderr. A
stray `print()` corrupts the protocol stream, so keep stdout clean when contributing.

## Support

- API reference — <https://apidocs.photoncommerce.com/>
- Bugs and feature requests — [GitHub issues](https://github.com/Photon-Commerce/Photon-MCP/issues)
- Account, editing and async processing — <support@photoncommerce.com>

## License

MIT — see [LICENSE](LICENSE).
