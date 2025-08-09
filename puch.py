from typing import Annotated
from pydantic import Field
from pathlib import Path
import pypandoc
import pdfplumber
import httpx
import json
from mcp import ErrorData, McpError
from mcp.server.auth.provider import AccessToken
from mcp.types import TextContent, INVALID_PARAMS, INTERNAL_ERROR
from openai import BaseModel
from fastmcp import FastMCP
from fastmcp.server.auth.providers.bearer import BearerAuthProvider, RSAKeyPair

# ----------------- CONFIG -----------------
TOKEN = "<YOUR_APPLICATION_KEY>"   # your app key (also used for server auth)
MY_NUMBER = "919XXXXXXXXX"        # your phone number in {country}{number}
# Puch API endpoint where we submit the resume markdown (configure from Puch docs)
# Example: "https://api.puch.ai/mcp/receive_resume" (replace with the real endpoint)
PUCH_MCP_ENDPOINT = "https://puch.example.com/mcp/receive_resume"
# ------------------------------------------

class RichToolDescription(BaseModel):
    description: str
    use_when: str
    side_effects: str | None

class SimpleBearerAuthProvider(BearerAuthProvider):
    def __init__(self, token: str):
        k = RSAKeyPair.generate()
        super().__init__(public_key=k.public_key, jwks_uri=None, issuer=None, audience=None)
        self.token = token

    async def load_access_token(self, token: str) -> AccessToken | None:
        if token == self.token:
            return AccessToken(token=token, client_id="unknown", scopes=[], expires_at=None)
        return None

mcp = FastMCP("My MCP Server", auth=SimpleBearerAuthProvider(TOKEN))

ResumeToolDescription = RichToolDescription(
    description="Serve your resume in plain markdown.",
    use_when="Return raw markdown of your resume; also submit it to Puch endpoint.",
    side_effects="This tool will POST the resume markdown to the configured Puch endpoint as a side-effect."
)

def pdf_to_markdown(path: Path) -> str:
    """Simple extraction of text from PDF, formatted into paragraphs as markdown."""
    text_parts = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text.strip())
    text = "\n\n".join(text_parts).strip()
    # Very light normalization to Markdown (leave plain text; headings not inferred)
    return text

async def submit_to_puch(endpoint: str, app_key: str, phone: str, markdown_text: str) -> tuple[int, str]:
    """Submit the resume markdown to the Puch MCP endpoint. Returns (status_code, response_text)."""
    headers = {
        "Authorization": f"Bearer {app_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "phone": phone,
        "resume_markdown": markdown_text,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(endpoint, headers=headers, json=payload)
            return resp.status_code, resp.text
        except httpx.HTTPError as e:
            # bubble up as a tuple; caller will handle
            return 0, str(e)

@mcp.tool(description=ResumeToolDescription.model_dump_json())
async def resume(
    resume_path: Annotated[str, Field(description="Local path to your resume file (pdf/docx/md/txt)")] = "resume.pdf"
) -> str:
    """
    Reads a local resume file, converts to markdown string, POSTS to Puch endpoint,
    and returns the raw markdown text (no extra formatting).
    """
    try:
        path = Path(resume_path).expanduser()
        if not path.exists():
            return "<error>Resume file not found at: {}</error>".format(str(path))

        suffix = path.suffix.lower()

        # 1) Convert file to markdown string
        markdown_text = ""
        try:
            if suffix == ".pdf":
                # Use pdfplumber to extract text and keep simple paragraphs
                markdown_text = pdf_to_markdown(path)
            elif suffix in {".docx", ".doc", ".odt", ".md", ".txt"}:
                # pypandoc handles these formats; pandoc must be installed on OS
                # For .md or .txt, this will simply return text; md is preserved.
                markdown_text = pypandoc.convert_file(str(path), 'md')
            else:
                # fallback: try pypandoc, otherwise plain text
                try:
                    markdown_text = pypandoc.convert_file(str(path), 'md')
                except Exception:
                    markdown_text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            # conversion failed
            return f"<error>Failed to convert resume to markdown: {e}</error>"

        if not markdown_text or markdown_text.strip() == "":
            return "<error>Converted resume is empty.</error>"

        # 2) Submit to Puch endpoint (side-effect)
        if PUCH_MCP_ENDPOINT and PUCH_MCP_ENDPOINT.startswith("http"):
            status_code, resp_text = await submit_to_puch(PUCH_MCP_ENDPOINT, TOKEN, MY_NUMBER, markdown_text)
            if status_code == 0:
                # network error
                # still return markdown (tool's primary contract) but include error as comment (not allowed!)
                # **We MUST return raw markdown only**, so we won't append error to the markdown.
                # Instead, log to stderr via raising McpError so Puch sees failure if necessary.
                raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Failed to POST resume to Puch endpoint: {resp_text}"))
            elif status_code >= 400:
                raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Puch endpoint returned {status_code}: {resp_text}"))
            # else success; continue to return markdown

        # 3) Return the markdown string (exactly raw markdown)
        return markdown_text

    except McpError:
        # re-raise MCP errors unchanged
        raise
    except Exception as e:
        # Any other unexpected error
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=f"Unexpected error in resume tool: {e}"))
