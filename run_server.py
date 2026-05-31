#!/usr/bin/env python3
"""Run the BIG-IP Telemetry Exporter API."""

import os

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8001"))
    uvicorn.run(
        "backend.app:app",
        host="0.0.0.0",
        port=port,
        reload=False,
    )
