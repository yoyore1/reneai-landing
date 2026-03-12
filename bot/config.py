"""
Configuration loaded from environment / .env file.
"""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    poly_api_key: str = os.getenv("POLY_API_KEY", "")
    poly_api_secret: str = os.getenv("POLY_API_SECRET", "")
    poly_api_passphrase: str = os.getenv("POLY_API_PASSPHRASE", "")
    poly_private_key: str = os.getenv("POLY_PRIVATE_KEY", "")
    poly_signature_type: int = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))
    poly_funder_address: str = os.getenv("POLY_FUNDER_ADDRESS", "")

    max_position_usdc: float = float(os.getenv("MAX_POSITION_USDC", "50.0"))
    dry_run: bool = os.getenv("DRY_RUN", "true").lower() == "true"

    poly_clob_host: str = "https://clob.polymarket.com"
    chain_id: int = 137

    # Email alerts
    email_to: str = os.getenv("EMAIL_TO", "")
    email_from: str = os.getenv("EMAIL_FROM", "")
    email_user: str = os.getenv("EMAIL_USER", "")
    email_password: str = os.getenv("EMAIL_PASSWORD", "")
    smtp_host: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))


cfg = Config()
