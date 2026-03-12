"""
Email notifications (losses only). Configure via .env to enable.
"""

import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from bot.config import cfg

log = logging.getLogger("notify")


def send_loss_email(subject: str, body: str) -> None:
    """Send one email for a loss event. No-op if email not configured."""
    enabled = getattr(cfg, "email_notify_losses", False)
    if not enabled:
        return
    to_addr = getattr(cfg, "email_to", "").strip()
    if not to_addr:
        return
    host = getattr(cfg, "smtp_host", "").strip()
    user = getattr(cfg, "email_user", "").strip()
    password = getattr(cfg, "email_password", "").strip()
    from_addr = getattr(cfg, "email_from", "").strip() or user
    port = getattr(cfg, "smtp_port", 587)
    if not host or not user or not password:
        log.debug("Email not sent: missing SMTP config")
        return
    try:
        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(host, port, timeout=10) as s:
            s.starttls()
            s.login(user, password)
            s.sendmail(from_addr, [to_addr], msg.as_string())
        log.info("Loss email sent: %s", subject[:50])
    except Exception as exc:
        log.warning("Failed to send loss email: %s", exc)
