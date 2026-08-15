"""Email sending.
- Family invites via Resend (dormant when RESEND_API_KEY empty).
- Password reset codes via SMTP (dormant when SMTP_HOST or credentials empty).
"""
import html as _html
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

try:
    import resend  # type: ignore
    _RESEND_AVAILABLE = True
except Exception:
    resend = None  # type: ignore
    _RESEND_AVAILABLE = False


def _resend_configured() -> bool:
    return _RESEND_AVAILABLE and bool(settings.resend_api_key)


if _resend_configured():
    resend.api_key = settings.resend_api_key  # type: ignore


def send_family_invite(inviter_name: str, invitee_email: str, relationship: str = "family") -> Optional[str]:
    if not _resend_configured():
        logger.info(f"[email] RESEND_API_KEY missing - skipping invite email to {invitee_email}")
        return None

    invite_link = settings.app_invite_url
    safe_name = _html.escape((inviter_name or "Someone").strip()[:80], quote=True) or "Someone"
    safe_email = _html.escape((invitee_email or "").strip()[:254], quote=True)
    safe_rel = _html.escape((relationship or "family").capitalize()[:20], quote=True)
    subject_name = (inviter_name or "Someone").strip().replace("\r", " ").replace("\n", " ")[:60]
    subject = f"{subject_name} invited you to Folin"
    html = f"""
    <!DOCTYPE html>
    <html>
      <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:#F4F4F6; padding:24px; margin:0;">
        <div style="max-width:520px;margin:0 auto;background:#FFFFFF;border-radius:20px;padding:32px 28px;">
          <div style="text-align:center;">
            <div style="display:inline-block;background:#FDE4CE;color:#B04B0F;font-weight:800;padding:10px 14px;border-radius:16px;font-size:18px;">Folin</div>
          </div>
          <h1 style="font-size:22px;color:#17181A;margin:24px 0 8px 0;letter-spacing:-0.4px;">
            {safe_name} invited you to share health scores
          </h1>
          <p style="font-size:14px;color:#5A5F6A;line-height:22px;margin:0 0 20px 0;">
            They'd like you to be part of their <strong>{safe_rel}</strong> circle on Folin. You'll never see raw lab values, only the summary score.
          </p>
          <div style="text-align:center;margin:28px 0 20px;">
            <a href="{invite_link}" style="display:inline-block;background:#F1580C;color:#FFFFFF;text-decoration:none;font-weight:700;padding:14px 28px;border-radius:999px;font-size:15px;">
              Open Folin
            </a>
          </div>
          <p style="font-size:13px;color:#5A5F6A;line-height:20px;margin:0 0 6px 0;">
            Sign up (or log in) using <strong>{safe_email}</strong> to accept.
          </p>
        </div>
      </body>
    </html>
    """
    try:
        resp = resend.Emails.send({  # type: ignore
            "from": settings.resend_from,
            "to": [invitee_email],
            "subject": subject,
            "html": html,
        })
        email_id = (resp or {}).get("id") if isinstance(resp, dict) else getattr(resp, "id", None)
        logger.info(f"[email] Sent invite to {invitee_email} · id={email_id}")
        return email_id
    except Exception as e:
        logger.warning(f"[email] Failed to send invite to {invitee_email}: {e}")
        return None


# ---------- Password reset via SMTP () ----------
def smtp_enabled() -> bool:
    return bool(settings.smtp_enabled)


def send_password_reset(to_email: str, code: str) -> bool:
    """Send a 6-digit reset code via SMTP. Returns True on best-effort success.
    Uses stdlib smtplib - no new dependencies.
    """
    if not smtp_enabled():
        logger.info(f"[email] SMTP not configured - skipping reset email to {to_email}")
        return False
    subject = f"Your Folin password reset code: {code}"
    text = (
        f"Your Folin reset code is: {code}\n\n"
        "This code is valid for 15 minutes and can only be used once. If you didn't request "
        "this, you can safely ignore this email.\n\n- Folin"
    )
    html = f"""
    <!DOCTYPE html>
    <html>
      <body style="font-family:-apple-system,'Segoe UI',sans-serif;background:#F4F4F6;padding:24px;margin:0;">
        <div style="max-width:480px;margin:0 auto;background:#FFF;border-radius:20px;padding:32px 28px;">
          <div style="text-align:center;background:#FDE4CE;color:#B04B0F;font-weight:800;padding:10px 14px;border-radius:16px;font-size:18px;display:inline-block;">Folin</div>
          <h1 style="font-size:20px;color:#17181A;margin:24px 0 8px 0;">Reset your password</h1>
          <p style="font-size:14px;color:#5A5F6A;line-height:22px;margin:0 0 20px 0;">Enter this code in the app to set a new password. It expires in 15 minutes.</p>
          <div style="text-align:center;font-size:32px;font-weight:800;letter-spacing:8px;color:#17181A;background:#F4F4F6;padding:18px 8px;border-radius:14px;">{_html.escape(code)}</div>
          <p style="font-size:12px;color:#8A8F98;line-height:18px;margin:22px 0 0 0;">If you didn't request this, you can ignore it.</p>
        </div>
      </body>
    </html>
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = to_email
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as s:
            s.ehlo()
            try:
                s.starttls()
                s.ehlo()
            except Exception:
                pass
            s.login(settings.smtp_user, settings.smtp_pass)
            s.sendmail(settings.smtp_from, [to_email], msg.as_string())
        logger.info(f"[email] Sent reset code to {to_email}")
        return True
    except Exception as e:
        logger.warning(f"[email] SMTP send failed to {to_email}: {e}")
        return False
