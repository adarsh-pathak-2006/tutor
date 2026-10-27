"""Business rules for accounts and consent. API views stay thin and call these."""

import hmac
import re
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from apps.catalogue.models import Board, ClassLevel
from apps.core import messaging
from apps.core.errors import ApiError
from apps.operations import audit

from . import lockout
from .models import (
    ApprovalRequest,
    ConsentRecord,
    ConsentText,
    GuardianLink,
    ParentProfile,
    StudentProfile,
    User,
    VerificationCode,
)
from .tokens import hash_secret, make_link_token, make_otp

OTP_SENDS_PER_10_MIN = 5


# --- contacts -----------------------------------------------------------------


def normalize_contact(value: str) -> tuple[str, str]:
    """Return ("email", "a@b.com") or ("mobile", "+919876543210"). 10-digit numbers get +91."""
    value = (value or "").strip()
    if "@" in value:
        try:
            validate_email(value)
        except DjangoValidationError:
            raise ApiError(400, "validation_error", "Enter a valid email address.") from None
        return "email", value.lower()
    digits = re.sub(r"[\s\-()]", "", value)
    if re.fullmatch(r"\d{10}", digits):
        digits = "+91" + digits
    elif re.fullmatch(r"91\d{10}", digits):
        digits = "+" + digits
    if not re.fullmatch(r"\+[1-9]\d{7,14}", digits):
        raise ApiError(400, "validation_error", "Enter a valid email or mobile number.")
    return "mobile", digits


def find_user_by_contact(kind: str, value: str):
    return User.objects.filter(**{kind: value}, deleted_at__isnull=True).first()


def _clean_own_contacts(email, mobile):
    email = normalize_contact(email)[1] if email else None
    mobile = normalize_contact(mobile)[1] if mobile else None
    if email and User.objects.filter(email=email).exists():
        raise ApiError(409, "already_registered", "This email is already registered.", {"email": "taken"})
    if mobile and User.objects.filter(mobile=mobile).exists():
        raise ApiError(409, "already_registered", "This mobile number is already registered.", {"mobile": "taken"})
    return email, mobile


def _check_password(password, full_name, email):
    try:
        validate_password(password, user=User(full_name=full_name, email=email))
    except DjangoValidationError as exc:
        raise ApiError(
            400, "validation_error", "Choose a stronger password.", {"password": " ".join(exc.messages)}
        ) from None


def _class_and_board(class_number, board_code):
    class_level = ClassLevel.objects.filter(number=class_number).first()
    board = Board.objects.filter(code=(board_code or "").upper()).first()
    fields = {}
    if not class_level:
        fields["class_number"] = "Choose a class from 6 to 12."
    if not board:
        fields["board_code"] = "Unknown board."
    if fields:
        raise ApiError(400, "validation_error", "Some fields are invalid.", fields)
    return class_level, board


# --- one-time codes -------------------------------------------------------------


def issue_otp(destination: str, purpose: str) -> None:
    kind, value = normalize_contact(destination)
    since = timezone.now() - timedelta(minutes=10)
    if VerificationCode.objects.filter(destination=value, created_at__gte=since).count() >= OTP_SENDS_PER_10_MIN:
        raise ApiError(429, "limit_reached", "Too many codes requested. Try again in a few minutes.")
    # Codes for unknown contacts are silently not sent, so the API never reveals who is registered.
    if not find_user_by_contact(kind, value):
        return
    raw, hashed = make_otp()
    VerificationCode.objects.create(
        destination=value,
        purpose=purpose,
        code_hash=hashed,
        expires_at=timezone.now() + timedelta(minutes=VerificationCode.VALID_MINUTES),
    )
    messaging.send("email" if kind == "email" else "sms", value, f"Your TUTOR code is {raw}. It expires in 10 minutes.")


def check_otp(destination: str, purpose: str, code: str):
    """Return the user if the code is right, else raise. Marks contact verified for verify_contact."""
    kind, value = normalize_contact(destination)
    vc = (
        VerificationCode.objects.filter(
            destination=value, purpose=purpose, used_at__isnull=True, expires_at__gt=timezone.now()
        )
        .order_by("-created_at")
        .first()
    )
    if not vc or vc.attempts >= VerificationCode.MAX_ATTEMPTS:
        raise ApiError(400, "invalid_code", "The code is wrong or has expired.")
    if not hmac.compare_digest(vc.code_hash, hash_secret((code or "").strip())):
        vc.attempts += 1
        vc.save(update_fields=["attempts"])
        raise ApiError(400, "invalid_code", "The code is wrong or has expired.")
    vc.used_at = timezone.now()
    vc.save(update_fields=["used_at"])
    user = find_user_by_contact(kind, value)
    if user is None:
        raise ApiError(400, "invalid_code", "The code is wrong or has expired.")
    if purpose == "verify_contact":
        setattr(user, f"{kind}_verified_at", timezone.now())
        user.save(update_fields=[f"{kind}_verified_at"])
    return user


def reset_password(destination, code, new_password):
    user = check_otp(destination, "reset_password", code)
    _check_password(new_password, user.full_name, user.email)
    user.set_password(new_password)
    user.save(update_fields=["password"])
    for identifier in (user.email, user.mobile):  # proving ownership of the contact lifts a login lockout
        if identifier:
            lockout.clear(identifier)
    audit.record(user, "user.password_reset", user)
    return user


def _send_own_verification(user):
    target = user.mobile or user.email
    issue_otp(target, "verify_contact")


# --- sign-up ---------------------------------------------------------------------


@transaction.atomic
def signup_student(data, ip=None):
    email, mobile = _clean_own_contacts(data.email, data.mobile)
    parent_kind, parent_value = normalize_contact(data.parent_contact)
    if parent_value in (email, mobile):
        raise ApiError(
            400, "validation_error", "Enter your parent's contact, not your own.", {"parent_contact": "same as student"}
        )
    class_level, board = _class_and_board(data.class_number, data.board_code)
    _check_password(data.password, data.full_name, email)
    user = User.objects.create_user(
        email=email, mobile=mobile, password=data.password, full_name=data.full_name.strip(), account_type="student"
    )
    StudentProfile.objects.create(
        user=user, class_level=class_level, board=board, city=data.city.strip(), school_name=data.school_name.strip()
    )
    create_approval_request(user, parent_kind, parent_value)
    audit.record(user, "student.signup", user, ip=ip)
    _send_own_verification(user)
    return user


@transaction.atomic
def signup_parent(data, ip=None):
    email, mobile = _clean_own_contacts(data.email, data.mobile)
    _check_password(data.password, data.full_name, email)
    user = User.objects.create_user(
        email=email, mobile=mobile, password=data.password, full_name=data.full_name.strip(), account_type="parent"
    )
    ParentProfile.objects.create(user=user, preferred_language=data.preferred_language)
    audit.record(user, "parent.signup", user, ip=ip)
    _send_own_verification(user)
    return user


# --- parent approval ---------------------------------------------------------------


def create_approval_request(student, kind, contact):
    raw, hashed = make_link_token()
    req = ApprovalRequest.objects.create(
        student=student,
        parent_contact=contact,
        channel="email" if kind == "email" else "sms",
        token_hash=hashed,
        expires_at=timezone.now() + timedelta(days=ApprovalRequest.VALID_DAYS),
    )
    _send_approval_link(req, raw)
    return req


def _send_approval_link(req, raw_token):
    link = f"{settings.FRONTEND_URL}/approve/{raw_token}"
    messaging.send(
        req.channel,
        req.parent_contact,
        f"{req.student.full_name} has asked to join TUTOR. Review and approve: {link}",
    )


def resend_approval(student):
    profile = _student_profile(student)
    if profile.status != StudentProfile.Status.AWAITING_CONSENT:
        raise ApiError(409, "conflict", "Your account doesn't need approval.")
    req = student.approval_requests.filter(status=ApprovalRequest.Status.SENT).first()
    if not req:
        raise ApiError(404, "not_found", "No approval request to resend.")
    now = timezone.now()
    if req.last_sent_at < now - timedelta(days=1):
        req.send_count = 0
    if req.send_count >= ApprovalRequest.MAX_SENDS_PER_DAY:
        raise ApiError(429, "limit_reached", "You can resend the link 3 times a day.")
    raw, hashed = make_link_token()
    req.token_hash = hashed
    req.send_count += 1
    req.last_sent_at = now
    req.expires_at = now + timedelta(days=ApprovalRequest.VALID_DAYS)
    req.save()
    _send_approval_link(req, raw)


@transaction.atomic
def change_parent_contact(student, new_contact):
    profile = _student_profile(student)
    if profile.status != StudentProfile.Status.AWAITING_CONSENT:
        raise ApiError(409, "conflict", "Your account doesn't need approval.")
    kind, value = normalize_contact(new_contact)
    if value in (student.email, student.mobile):
        raise ApiError(400, "validation_error", "Enter your parent's contact, not your own.")
    student.approval_requests.filter(status=ApprovalRequest.Status.SENT).update(status=ApprovalRequest.Status.EXPIRED)
    create_approval_request(student, kind, value)


def get_request_for_token(raw_token):
    req = (
        ApprovalRequest.objects.select_related("student__student_profile__class_level")
        .filter(token_hash=hash_secret(raw_token or ""))
        .first()
    )
    if not req:
        raise ApiError(404, "not_found", "This link is not valid.")
    if req.status == ApprovalRequest.Status.SENT and req.expires_at <= timezone.now():
        req.status = ApprovalRequest.Status.EXPIRED
        req.save(update_fields=["status"])
    if not req.is_usable:
        raise ApiError(410, "link_expired", "This link has expired or was already used.")
    return req


def _require_parent(parent):
    if not hasattr(parent, "parent_profile"):
        raise ApiError(403, "forbidden", "Only a parent account can do this.")
    if not (parent.email_verified_at or parent.mobile_verified_at):
        raise ApiError(403, "contact_not_verified", "Verify your mobile or email first.")


def _current_consent_text():
    text = ConsentText.current()
    if not text:
        raise ApiError(503, "consent_text_missing", "Consent text is not set up yet.")
    return text


@transaction.atomic
def approve_request(req, parent, relationship, ip=None):
    _require_parent(parent)
    parent_contacts = set()
    for contact in (parent.email, parent.mobile):
        if contact:
            try:
                parent_contacts.add(normalize_contact(contact)[1])
            except ApiError:
                parent_contacts.add(contact.strip().lower())
    if req.parent_contact not in parent_contacts:
        raise ApiError(403, "forbidden", "This approval link was sent to a different parent contact.")
    student = req.student
    if parent.pk == student.pk:
        raise ApiError(403, "forbidden", "A student can't approve their own account.")
    text = _current_consent_text()
    link = GuardianLink.objects.filter(student=student, is_primary=True, ended_at__isnull=True).first()
    if link and link.parent_id != parent.pk:
        raise ApiError(409, "already_has_guardian", "This child already has a guardian on TUTOR.")
    if not link:
        link = GuardianLink.objects.create(parent=parent, student=student, relationship=relationship)
    consent = ConsentRecord.objects.create(guardian_link=link, consent_text=text, given_ip=ip)
    req.status = ApprovalRequest.Status.APPROVED
    req.approved_by = parent
    req.save(update_fields=["status", "approved_by"])
    profile = student.student_profile
    profile.status = StudentProfile.Status.ACTIVE
    profile.save(update_fields=["status", "updated_at"])
    audit.record(parent, "consent.given", consent, after={"student": str(student.pk), "text": text.version}, ip=ip)
    return link


def report_request(req, ip=None):
    req.status = ApprovalRequest.Status.REPORTED
    req.save(update_fields=["status"])
    audit.record(None, "approval.reported", req, ip=ip)


@transaction.atomic
def add_child(parent, data, ip=None):
    _require_parent(parent)
    email, mobile = _clean_own_contacts(data.email, data.mobile)
    class_level, board = _class_and_board(data.class_number, data.board_code)
    _check_password(data.password, data.full_name, email)
    text = _current_consent_text()
    child = User.objects.create_user(
        email=email, mobile=mobile, password=data.password, full_name=data.full_name.strip(), account_type="student"
    )
    StudentProfile.objects.create(
        user=child,
        class_level=class_level,
        board=board,
        city=data.city.strip(),
        school_name=data.school_name.strip(),
        status=StudentProfile.Status.ACTIVE,
    )
    link = GuardianLink.objects.create(parent=parent, student=child, relationship=data.relationship)
    consent = ConsentRecord.objects.create(guardian_link=link, consent_text=text, given_ip=ip)
    audit.record(parent, "consent.given", consent, after={"student": str(child.pk), "text": text.version}, ip=ip)
    return child


def _parent_link(parent, student_id):
    link = (
        GuardianLink.objects.select_related("student__student_profile")
        .filter(parent=parent, student_id=student_id, ended_at__isnull=True)
        .first()
    )
    if not link:
        raise ApiError(404, "not_found", "Child not found.")
    return link


@transaction.atomic
def withdraw_consent(parent, student_id, ip=None):
    link = _parent_link(parent, student_id)
    consent = link.consents.filter(withdrawn_at__isnull=True).first()
    if not consent:
        raise ApiError(409, "conflict", "There is no active consent to withdraw.")
    consent.withdrawn_at = timezone.now()
    consent.save(update_fields=["withdrawn_at"])
    profile = link.student.student_profile
    profile.status = StudentProfile.Status.PAUSED
    profile.save(update_fields=["status", "updated_at"])
    audit.record(parent, "consent.withdrawn", consent, ip=ip)


@transaction.atomic
def restore_consent(parent, student_id, ip=None):
    link = _parent_link(parent, student_id)
    profile = link.student.student_profile
    if profile.status != StudentProfile.Status.PAUSED:
        raise ApiError(409, "conflict", "Consent is not withdrawn.")
    consent = ConsentRecord.objects.create(guardian_link=link, consent_text=_current_consent_text(), given_ip=ip)
    profile.status = StudentProfile.Status.ACTIVE
    profile.save(update_fields=["status", "updated_at"])
    audit.record(parent, "consent.restored", consent, ip=ip)


# --- /me -------------------------------------------------------------------------------


def _student_profile(user):
    if not hasattr(user, "student_profile"):
        raise ApiError(403, "forbidden", "Only a student account can do this.")
    return user.student_profile


def me_payload(user):
    student = parent = None
    if hasattr(user, "student_profile"):
        sp = user.student_profile
        student = {
            "class_number": sp.class_level.number,
            "board": sp.board.code,
            "city": sp.city,
            "school_name": sp.school_name,
            "status": sp.status,
        }
    children = []
    if hasattr(user, "parent_profile"):
        pp = user.parent_profile
        parent = {"preferred_language": pp.preferred_language, "notify_by": pp.notify_by}
        links = GuardianLink.objects.filter(parent=user, ended_at__isnull=True).select_related(
            "student__student_profile__class_level"
        )
        children = [
            {
                "id": link.student.pk,
                "full_name": link.student.full_name,
                "class_number": link.student.student_profile.class_level.number,
                "status": link.student.student_profile.status,
            }
            for link in links
        ]
    roles = [
        {
            "role": g.role,
            "subject": g.subject.slug if g.subject else None,
            "class_number": g.class_level.number if g.class_level else None,
        }
        for g in user.role_grants.filter(revoked_at__isnull=True).select_related("subject", "class_level")
    ]
    return {
        "id": user.pk,
        "full_name": user.full_name,
        "email": user.email,
        "mobile": user.mobile,
        "email_verified": bool(user.email_verified_at),
        "mobile_verified": bool(user.mobile_verified_at),
        "account_type": user.account_type,
        "student": student,
        "parent": parent,
        "children": children,
        "roles": roles,
    }
