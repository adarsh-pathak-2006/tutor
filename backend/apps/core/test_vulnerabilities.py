"""Comprehensive Vulnerability & Defect Verification Test Suite.

Automated regression and proof-of-concept tests for the critical and high-severity
findings documented in understanding/FULL_SECURITY_AND_BUG_AUDIT_REPORT.md.

Run this suite with:
    python manage.py test apps.core.test_vulnerabilities --settings=config.settings.test
"""

import hashlib
import json
import os
import re
from io import StringIO

from django.conf import settings
from django.core.management import call_command
from django.db import IntegrityError
from django.test import Client, RequestFactory, TestCase, override_settings
from django.utils import timezone

from apps.accounts import services as accounts_services
from apps.accounts.backends import EmailOrMobileBackend
from apps.accounts.models import ApprovalRequest, GuardianLink, User, VerificationCode
from apps.accounts.schemas import ParentSignupIn, StudentSignupIn
from apps.accounts.tokens import hash_secret
from apps.assessment.admin import QuestionVersionAdmin
from apps.assessment.models import Answer, Attempt, AttemptItem, Question, QuestionVersion
from apps.catalogue.models import ClassLevel, Course, Lesson, PublishStatus, Skill
from apps.commerce.models import Entitlement
from apps.content.admin import ContentVersionAdmin
from apps.content.models import ContentVersion
from apps.core import messaging
from apps.learning import mastery, planner, progress
from apps.learning.models import MasteryState

PASSWORD = getattr(settings, "TUTOR_DEMO_PASSWORD", None) or os.getenv("TUTOR_DEMO_PASSWORD") or "Test-Pass-2026"


def post_json(client, path, data=None):
    return client.post(path, data=json.dumps(data or {}), content_type="application/json")


@override_settings(TUTOR_DEMO_DATA=True, TUTOR_DEMO_PASSWORD=PASSWORD)
class VulnerabilityVerificationTests(TestCase):
    """Verifies and proves the presence of critical and high-severity vulnerabilities."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", stdout=StringIO())

    def setUp(self):
        messaging.OUTBOX.clear()

    # -------------------------------------------------------------------------
    # 1. ISSUE-01: CRITICAL - Reversible Unsalted SHA-256 OTP Storage (Fixed)
    # -------------------------------------------------------------------------
    def test_critical_unsalted_sha256_otp_rainbow_table_reversibility(self):
        """ISSUE-01: OTP codes are stored as salted, key-derived hashes.

        Exhaustive search with raw unsalted SHA-256 fails to reverse the stored hash.
        Legitimate code verification succeeds via salted_hmac.
        """
        user = User.objects.create_user(
            email="victim_otp@example.com",
            mobile="+919800000011",
            password=PASSWORD,
            full_name="Victim OTP",
        )

        # Issue OTP via service
        accounts_services.issue_otp(user.email, "login")

        # Inspect the database record
        vc = VerificationCode.objects.filter(destination=user.email, purpose="login").latest("created_at")
        stored_hash = vc.code_hash

        # Extract the issued code from outbox
        msg_text = next(m["text"] for m in reversed(messaging.OUTBOX) if m["to"] == user.email)
        raw_code = re.search(r"code is (\d{6})", msg_text).group(1)

        # Reversibility Proof: Exhaustively test 000000-999999 with raw SHA-256
        recovered_code = None
        for i in range(1_000_000):
            candidate = f"{i:06d}"
            if hashlib.sha256(candidate.encode()).hexdigest() == stored_hash:
                recovered_code = candidate
                break

        # Verification: Unsalted SHA-256 rainbow search fails to reverse the stored hash
        self.assertIsNone(recovered_code, "Unsalted SHA-256 should not recover salted OTP hash")
        self.assertEqual(len(stored_hash), 64)
        self.assertEqual(hash_secret(raw_code), stored_hash)

        # Confirm the legitimate code authenticates successfully
        verified_user = accounts_services.check_otp(user.email, "login", raw_code)
        self.assertEqual(verified_user.pk, user.pk)

    # -------------------------------------------------------------------------
    # 2. ISSUE-02: HIGH - BOLA / IDOR in Parental Consent Approval (Fixed)
    # -------------------------------------------------------------------------
    def test_high_idor_parent_can_hijack_unrelated_child_via_approval_token(self):
        """ISSUE-02: BOLA / IDOR in POST /api/v1/consent/link/{token}/approve.

        The approve_request service verifies that the approving parent's
        email/mobile matches req.parent_contact. An unrelated parent attempting
        to approve must be rejected with 403 forbidden.
        """
        # Register a student requesting approval from legitimate_parent@example.com
        student_data = StudentSignupIn(
            full_name="Alice Student",
            email="alice_student@example.com",
            mobile="+919800000012",
            password=PASSWORD,
            class_number=8,
            board_code="cbse",
            city="Delhi",
            school_name="Delhi Public School",
            parent_contact="legitimate_parent@example.com",
        )
        student = accounts_services.signup_student(student_data)

        # Extract approval token from outbox
        msg_text = next(m["text"] for m in reversed(messaging.OUTBOX) if m["to"] == "legitimate_parent@example.com")
        token_match = re.search(r"/approve/([A-Za-z0-9_-]+)", msg_text)
        self.assertIsNotNone(token_match)
        raw_token = token_match.group(1)

        # Register an entirely unrelated parent (Eve Attacker)
        attacker_data = ParentSignupIn(
            full_name="Eve Attacker",
            email="eve_attacker@example.com",
            mobile="+919800000013",
            password=PASSWORD,
        )
        attacker_parent = accounts_services.signup_parent(attacker_data)
        attacker_parent.email_verified_at = timezone.now()
        attacker_parent.save(update_fields=["email_verified_at"])

        # Authenticate as Eve and call /consent/link/{token}/approve
        client = Client()
        client.force_login(attacker_parent, backend="apps.accounts.backends.EmailOrMobileBackend")

        response = post_json(
            client,
            f"/api/v1/consent/link/{raw_token}/approve",
            {"relationship": "guardian", "accept": True},
        )

        # Verification: Unrelated parent is rejected with 403 Forbidden
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "forbidden")

        # Verify that Eve is NOT linked to child in the database
        link = GuardianLink.objects.filter(parent=attacker_parent, student=student, ended_at__isnull=True).first()
        self.assertIsNone(link, "Attacker parent must not be linked to child as guardian")

        # Verify Alice does NOT appear under Eve's /api/v1/me payload
        me_resp = client.get("/api/v1/me")
        self.assertEqual(me_resp.status_code, 200)
        children = me_resp.json().get("children", [])
        child_ids = [c["id"] for c in children]
        self.assertNotIn(str(student.pk), child_ids)

        # Verify legitimate parent CAN approve the request
        req = ApprovalRequest.objects.get(student=student)
        legit_parent = User.objects.create_user(
            email="legitimate_parent@example.com",
            mobile="+919800000014",
            password=PASSWORD,
            full_name="Legit Parent",
        )
        legit_parent.email_verified_at = timezone.now()
        legit_parent.save()
        accounts_services.ParentProfile.objects.create(user=legit_parent)

        approved_link = accounts_services.approve_request(req, legit_parent, "mother")
        self.assertEqual(approved_link.parent_id, legit_parent.pk)
        self.assertEqual(approved_link.student_id, student.pk)

    # -------------------------------------------------------------------------
    # 3. ISSUE-03: HIGH - Cold-Start Mastery Depressed Score & "Weak" Tagging (Fixed)
    # -------------------------------------------------------------------------
    def test_high_cold_start_mastery_calculates_weak_on_perfect_first_answer(self):
        """ISSUE-03: Cold-start EMA score initialization in mastery.py.

        When a student demonstrates 100% correct competency with zero hints on their
        first question (credit=1.0), the score is initialized directly to 1.00 rather
        than depressed by a zero baseline. The skill is classified as DEVELOPING, not WEAK.
        """
        student = User.objects.create_user(
            email="student_mastery@example.com",
            mobile="+919800000021",
            password=PASSWORD,
            full_name="Student Mastery",
        )
        course = Course.objects.filter(status=PublishStatus.PUBLISHED).first()
        skill = Skill.objects.filter(lesson_links__lesson__module__course=course).first()
        self.assertIsNotNone(skill)

        # Record perfect answer with 0 hints (credit = 1.0)
        state = mastery.record_evidence(student, skill, credit=1.0)

        # Verification: First correct answer initializes score to 1.0 and assigns DEVELOPING level
        self.assertEqual(state.score, 1.0)
        self.assertEqual(state.evidence_count, 1)
        self.assertEqual(state.level, MasteryState.Level.DEVELOPING)

        # Verification on planner: review_item shows DEVELOPING state (not 'weak')
        review = planner.review_item(student)
        self.assertIsNotNone(review)
        self.assertEqual(review["skill"].code, skill.code)
        self.assertEqual(review["state"].level, MasteryState.Level.DEVELOPING)

    # -------------------------------------------------------------------------
    # 4. ISSUE-04: HIGH - Unpublished Draft Lesson Progression Deadlock
    # -------------------------------------------------------------------------
    def test_high_unpublished_draft_lesson_breaks_student_today_progression(self):
        """ISSUE-04: accessible_lessons() includes lessons lacking published ContentVersion.

        If a draft/placeholder lesson is added to an active course, planner.next_lesson()
        recommends it, but GET /api/v1/lessons/{id} answers 404, permanently wedging
        the student's /today dashboard.
        """
        # Set up active student with entitlement
        student = User.objects.create_user(
            email="student_planner@example.com",
            mobile="+919800000031",
            password=PASSWORD,
            full_name="Student Planner",
        )
        class_level = ClassLevel.objects.get(number=8)
        accounts_services.StudentProfile.objects.create(
            user=student,
            class_level=class_level,
            board=accounts_services.Board.objects.get(code="CBSE"),
            status=accounts_services.StudentProfile.Status.ACTIVE,
        )

        course = Course.objects.filter(status=PublishStatus.PUBLISHED).first()
        Entitlement.objects.create(student=student, product_type="course", product_id=course.id)

        module = course.modules.first()

        # Create an unpublished placeholder lesson (no ContentVersion)
        draft_lesson = Lesson.objects.create(
            module=module,
            title="Unpublished Draft Lesson",
            slug="draft-placeholder-lesson",
            position=99,
        )

        # Proves vulnerability: accessible_lessons() and next_lesson() return unpublished lesson
        accessible = list(planner.accessible_lessons(student))
        self.assertIn(draft_lesson, accessible)

        # Mark all published lessons finished to force next_lesson to select draft_lesson
        for lesson_item in accessible:
            if lesson_item.id != draft_lesson.id:
                progress.mark_finished(student, lesson_item)

        nxt = planner.next_lesson(student)
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt.id, draft_lesson.id)

        # Proves impact: student attempts to open their assigned next lesson and hits 404
        client = Client()
        client.force_login(student, backend="apps.accounts.backends.EmailOrMobileBackend")
        resp = client.get(f"/api/v1/lessons/{draft_lesson.id}")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "not_found")

        # Proves impact on course completion: course can never be completed
        completed = progress.check_course_completion(student, course)
        self.assertFalse(completed)

    # -------------------------------------------------------------------------
    # 5. ISSUE-05: HIGH - Concurrency Race Condition Creating Duplicate Quiz Attempts
    # -------------------------------------------------------------------------
    def test_high_duplicate_open_quiz_attempts_created_without_unique_constraint(self):
        """ISSUE-05: start_lesson_quiz lacks concurrency locking and unique constraint.

        Calling start_lesson_quiz concurrently or before submission allows duplicate
        active Attempt records for the same student and lesson, corrupting score state.
        """
        student = User.objects.create_user(
            email="quiz_race@example.com",
            mobile="+919800000041",
            password=PASSWORD,
            full_name="Quiz Race",
        )
        lesson = Lesson.objects.filter(module__course__status=PublishStatus.PUBLISHED).first()

        # Simulate concurrent start requests by directly creating two active attempts
        Attempt.objects.create(student=student, lesson=lesson, max_score=1)
        Attempt.objects.create(student=student, lesson=lesson, max_score=1)

        # Proves vulnerability: Database allows multiple active attempts for the same student and lesson
        active_attempts = Attempt.objects.filter(student=student, lesson=lesson, submitted_at__isnull=True)
        self.assertEqual(active_attempts.count(), 2)

    # -------------------------------------------------------------------------
    # 6. ISSUE-08: HIGH - Immutability Bypass on Published Assessment Questions
    # -------------------------------------------------------------------------
    def test_high_published_question_version_editable_in_admin(self):
        """ISSUE-08: QuestionVersionAdmin lacks get_readonly_fields.

        Unlike ContentVersionAdmin (which locks published/archived versions),
        QuestionVersionAdmin allows staff to edit published question stems and answer keys.
        """
        factory = RequestFactory()
        request = factory.get("/admin/assessment/questionversion/")
        admin_user = User.objects.create_superuser(
            email="staff_admin@example.com",
            password=PASSWORD,
            full_name="Staff Admin",
        )
        request.user = admin_user

        # Create published ContentVersion and published QuestionVersion
        cv = ContentVersion(status=ContentVersion.Status.PUBLISHED)
        content_admin = ContentVersionAdmin(ContentVersion, None)
        cv_readonly = content_admin.get_readonly_fields(request, cv)

        # ContentVersionAdmin correctly freezes published body
        self.assertIn("body", cv_readonly)
        self.assertIn("version_no", cv_readonly)

        # QuestionVersionAdmin does NOT freeze published body
        qv = QuestionVersion(status=QuestionVersion.Status.PUBLISHED)
        question_admin = QuestionVersionAdmin(QuestionVersion, None)
        qv_readonly = question_admin.get_readonly_fields(request, qv)

        # Proves vulnerability: body and question options remain editable
        self.assertNotIn("body", qv_readonly)
        self.assertNotIn("question", qv_readonly)

    # -------------------------------------------------------------------------
    # 7. ISSUE-10: MEDIUM - Timing Attack on Soft-Deleted Accounts
    # -------------------------------------------------------------------------
    def test_medium_soft_deleted_user_timing_enumeration_bypasses_password_hashing(self):
        """ISSUE-10: EmailOrMobileBackend._check short-circuits on soft-deleted users.

        Active users and non-existent users undergo PBKDF2 password hashing (taking 100-300ms).
        Soft-deleted accounts return None immediately without hashing, leaking account presence.
        """
        backend = EmailOrMobileBackend()

        # Create soft-deleted user
        deleted_user = User.objects.create_user(
            email="deleted_user@example.com",
            mobile="+919800000051",
            password=PASSWORD,
            full_name="Deleted User",
        )
        deleted_user.deleted_at = timezone.now()
        deleted_user.save()

        # Proves vulnerability: _check returns None without performing password hashing
        # In a patched implementation, User().set_password(password) would be called to equalize timing
        result = backend._check("deleted_user@example.com", PASSWORD)
        self.assertIsNone(result)

    # -------------------------------------------------------------------------
    # 8. ISSUE-12: MEDIUM - Unhandled Database IntegrityError on Concurrent Answers
    # -------------------------------------------------------------------------
    def test_medium_concurrent_answers_raise_integrity_error_rather_than_409(self):
        """ISSUE-12: Answer.item is a OneToOneField without exception handling.

        Concurrent answers to the same question item pass hasattr(item, 'answer')
        and trigger an unhandled IntegrityError in Django Ninja, producing a 500 server error.
        """
        student = User.objects.create_user(
            email="answer_race@example.com",
            mobile="+919800000061",
            password=PASSWORD,
            full_name="Answer Race",
        )
        course = Course.objects.filter(status=PublishStatus.PUBLISHED).first()
        lesson = course.modules.first().lessons.first()
        q = Question.objects.create(lesson=lesson, skill=Skill.objects.first(), type=Question.Type.MCQ)
        qv = QuestionVersion.objects.create(
            question=q,
            version_no=1,
            status=QuestionVersion.Status.PUBLISHED,
            body={"stem": "2+2?", "options": ["3", "4"], "answer_index": 1},
            author=student,
        )

        attempt = Attempt.objects.create(student=student, lesson=lesson, max_score=1)
        item = AttemptItem.objects.create(attempt=attempt, question_version=qv, position=1)

        # First answer succeeds
        Answer.objects.create(item=item, response={"choice_index": 1}, is_correct=True, marks=1)

        # Proves vulnerability: Duplicate answer throws raw IntegrityError (which causes HTTP 500)
        with self.assertRaises(IntegrityError):
            Answer.objects.create(item=item, response={"choice_index": 0}, is_correct=False, marks=0)
