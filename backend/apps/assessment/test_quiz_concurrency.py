import concurrent.futures
import os

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.accounts.models import StudentProfile, User
from apps.assessment import services
from apps.assessment.models import Attempt, AttemptItem, Question, QuestionVersion
from apps.catalogue.models import Board, ClassLevel, Course, Discipline, Lesson, Module, PublishStatus, Skill, Subject
from apps.commerce.models import Entitlement

PASSWORD = getattr(settings, "TUTOR_DEMO_PASSWORD", None) or os.getenv("TUTOR_DEMO_PASSWORD") or "Test-Pass-2026"


def _create_test_student(email, mobile):
    user = User.objects.create_user(
        email=email,
        mobile=mobile,
        password=PASSWORD,
        full_name="Quiz Test Student",
    )
    user.email_verified_at = timezone.now()
    user.save(update_fields=["email_verified_at"])
    class_level, _ = ClassLevel.objects.get_or_create(number=8)
    board, _ = Board.objects.get_or_create(code="CBSE", defaults={"name": "CBSE"})
    StudentProfile.objects.create(
        user=user,
        class_level=class_level,
        board=board,
        status=StudentProfile.Status.ACTIVE,
    )
    return user


def _create_test_course_and_lesson(slug_suffix=""):
    discipline, _ = Discipline.objects.get_or_create(name="Science", defaults={"slug": "science"})
    subject, _ = Subject.objects.get_or_create(discipline=discipline, name="Physics", defaults={"slug": "physics"})
    course = Course.objects.create(
        subject=subject,
        title=f"Test Course {slug_suffix}",
        slug=f"test-course-{slug_suffix}",
        status=PublishStatus.PUBLISHED,
    )
    module = Module.objects.create(course=course, title="Module 1", position=1)
    lesson = Lesson.objects.create(
        module=module,
        title=f"Test Lesson {slug_suffix}",
        slug=f"test-lesson-{slug_suffix}",
        position=1,
    )
    course.free_module = module
    course.save(update_fields=["free_module"])

    skill = Skill.objects.create(
        subject=subject,
        code=f"SKILL_{slug_suffix}"[:10],
        name="Test Skill",
        description="Skill description",
    )
    lesson.skill_links.create(skill=skill)

    author = User.objects.create_superuser(
        email=f"author_{slug_suffix}@example.com",
        password=PASSWORD,
        full_name="Author Admin",
    )

    for i in range(1, 4):
        q = Question.objects.create(
            lesson=lesson,
            skill=skill,
            type=Question.Type.MCQ,
            purpose=Question.Purpose.QUIZ,
            position=i,
        )
        QuestionVersion.objects.create(
            question=q,
            version_no=1,
            status=QuestionVersion.Status.PUBLISHED,
            body={
                "stem": f"Question {i} stem?",
                "options": ["A", "B", "C", "D"],
                "answer_index": 0,
                "hints": ["Hint 1"],
            },
            author=author,
        )
    return course, lesson


@override_settings(TUTOR_DEMO_DATA=False, PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AttemptDatabaseConstraintTests(TestCase):
    def setUp(self):
        self.student = _create_test_student("quiz_constraint_1@example.com", "+919800010001")
        self.other_student = _create_test_student("quiz_constraint_2@example.com", "+919800010002")
        _, self.lesson = _create_test_course_and_lesson("c1")
        _, self.other_lesson = _create_test_course_and_lesson("c2")

    def test_database_unique_constraint_rejects_duplicate_open_attempts(self):
        Attempt.objects.create(student=self.student, lesson=self.lesson, max_score=3)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            Attempt.objects.create(student=self.student, lesson=self.lesson, max_score=3)

    def test_database_allows_multiple_submitted_attempts(self):
        now = timezone.now()
        attempt1 = Attempt.objects.create(student=self.student, lesson=self.lesson, submitted_at=now, max_score=3)
        attempt2 = Attempt.objects.create(student=self.student, lesson=self.lesson, submitted_at=now, max_score=3)
        self.assertIsNotNone(attempt1.pk)
        self.assertIsNotNone(attempt2.pk)
        self.assertNotEqual(attempt1.pk, attempt2.pk)

    def test_database_allows_open_attempts_for_different_lessons(self):
        attempt1 = Attempt.objects.create(student=self.student, lesson=self.lesson, max_score=3)
        attempt2 = Attempt.objects.create(student=self.student, lesson=self.other_lesson, max_score=3)
        self.assertIsNotNone(attempt1.pk)
        self.assertIsNotNone(attempt2.pk)
        self.assertNotEqual(attempt1.pk, attempt2.pk)

    def test_database_allows_open_attempts_for_different_students(self):
        attempt1 = Attempt.objects.create(student=self.student, lesson=self.lesson, max_score=3)
        attempt2 = Attempt.objects.create(student=self.other_student, lesson=self.lesson, max_score=3)
        self.assertIsNotNone(attempt1.pk)
        self.assertIsNotNone(attempt2.pk)
        self.assertNotEqual(attempt1.pk, attempt2.pk)


@override_settings(TUTOR_DEMO_DATA=False, PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AttemptServiceIdempotencyTests(TestCase):
    def setUp(self):
        self.student = _create_test_student("quiz_idem_1@example.com", "+919800010011")
        self.course, self.lesson = _create_test_course_and_lesson("idem")
        Entitlement.objects.create(student=self.student, product_type="course", product_id=self.course.id)

    def test_start_lesson_quiz_idempotency_returns_same_instance(self):
        attempt1 = services.start_lesson_quiz(self.student, self.lesson)
        attempt2 = services.start_lesson_quiz(self.student, self.lesson)
        self.assertEqual(attempt1.id, attempt2.id)
        self.assertEqual(
            Attempt.objects.filter(student=self.student, lesson=self.lesson, submitted_at__isnull=True).count(),
            1,
        )

    def test_start_lesson_quiz_after_submission_allows_new_attempt(self):
        attempt1 = services.start_lesson_quiz(self.student, self.lesson)
        attempt1.submitted_at = timezone.now()
        attempt1.save(update_fields=["submitted_at"])

        attempt2 = services.start_lesson_quiz(self.student, self.lesson)
        self.assertNotEqual(attempt1.id, attempt2.id)
        self.assertIsNone(attempt2.submitted_at)


@override_settings(TUTOR_DEMO_DATA=False, PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AttemptConcurrencyBurstTests(TransactionTestCase):
    def setUp(self):
        self.student = _create_test_student("quiz_conc_1@example.com", "+919800010021")
        self.course, self.lesson = _create_test_course_and_lesson("conc")
        Entitlement.objects.create(student=self.student, product_type="course", product_id=self.course.id)

    def test_multithreaded_start_lesson_quiz_creates_exactly_one_attempt(self):
        num_threads = 8

        def worker():
            connection.close()
            try:
                att = services.start_lesson_quiz(self.student, self.lesson)
                return att.id
            finally:
                connection.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker) for _ in range(num_threads)]
            attempt_ids = [f.result() for f in concurrent.futures.as_completed(futures)]

        self.assertEqual(len(set(attempt_ids)), 1)

        open_attempts = Attempt.objects.filter(
            student=self.student,
            lesson=self.lesson,
            submitted_at__isnull=True,
        )
        self.assertEqual(open_attempts.count(), 1)
        self.assertEqual(AttemptItem.objects.filter(attempt_id=attempt_ids[0]).count(), 3)

    def test_api_endpoint_concurrent_start_requests_return_consistent_attempt(self):
        client = Client()
        client.force_login(self.student, backend="apps.accounts.backends.EmailOrMobileBackend")

        resp1 = client.post(f"/api/v1/lessons/{self.lesson.id}/quiz/start")
        self.assertEqual(resp1.status_code, 200)
        data1 = resp1.json()

        resp2 = client.post(f"/api/v1/lessons/{self.lesson.id}/quiz/start")
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()

        self.assertEqual(data1["attempt_id"], data2["attempt_id"])
        self.assertEqual(
            Attempt.objects.filter(student=self.student, lesson=self.lesson, submitted_at__isnull=True).count(),
            1,
        )
