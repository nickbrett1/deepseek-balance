"""Entry point: run the FastAPI app with uvicorn (python -m deepseek_balance)."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    port = int(os.environ.get("PORT", "3000"))
    uvicorn.run("deepseek_balance.app:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
