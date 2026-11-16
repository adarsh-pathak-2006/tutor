from django.core.management import call_command
from django.test import Client, TestCase

from apps.accounts.models import StudentProfile, User
from apps.catalogue.models import Board, ClassLevel, Course, Lesson, Skill
from apps.commerce.models import Entitlement
from apps.content.models import ContentVersion
from apps.learning import planner, progress
from apps.learning.models import CourseCompletion, MasteryState


class DraftLessonProgressionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo_catalogue", verbosity=0)
        cls.course = Course.objects.get(slug="ai-foundations")
        cls.module = cls.course.modules.first()
        cls.lesson1 = Lesson.objects.get(slug="what-is-data")
        cls.draft_lesson = Lesson.objects.create(
            module=cls.module,
            title="Unpublished Draft Placeholder",
            slug="draft-placeholder",
            position=99,
        )

    def setUp(self):
        self.client = Client()
        self.student = User.objects.create_user(
            email="student_draft@example.com",
            password="Draft-Pass-2026",
            full_name="Draft Student",
        )
        StudentProfile.objects.create(
            user=self.student,
            class_level=ClassLevel.objects.get(number=8),
            board=Board.objects.get(code="CBSE"),
            status=StudentProfile.Status.ACTIVE,
        )
        Entitlement.objects.create(student=self.student, product_type="course", product_id=self.course.id)
        self.client.force_login(self.student, backend="apps.accounts.backends.EmailOrMobileBackend")

    def test_accessible_lessons_excludes_draft_lesson(self):
        accessible = list(planner.accessible_lessons(self.student))
        self.assertIn(self.lesson1, accessible)
        self.assertNotIn(self.draft_lesson, accessible)

    def test_accessible_lessons_excludes_in_review_content_version(self):
        ContentVersion.objects.create(
            lesson=self.draft_lesson,
            version_no=1,
            status=ContentVersion.Status.IN_REVIEW,
            body={"sections": []},
            author=self.student,
        )
        accessible = list(planner.accessible_lessons(self.student))
        self.assertNotIn(self.draft_lesson, accessible)

    def test_accessible_lessons_includes_lesson_once_published(self):
        ContentVersion.objects.create(
            lesson=self.draft_lesson,
            version_no=1,
            status=ContentVersion.Status.PUBLISHED,
            body={"sections": []},
            author=self.student,
        )
        accessible = list(planner.accessible_lessons(self.student))
        self.assertIn(self.draft_lesson, accessible)

    def test_next_lesson_never_returns_draft_lesson(self):
        accessible = list(planner.accessible_lessons(self.student))
        for item in accessible:
            progress.mark_finished(self.student, item)

        nxt = planner.next_lesson(self.student)
        if nxt:
            self.assertNotEqual(nxt.id, self.draft_lesson.id)

    def test_today_endpoint_never_returns_draft_lesson(self):
        accessible = list(planner.accessible_lessons(self.student))
        for item in accessible:
            progress.mark_finished(self.student, item)

        resp = self.client.get("/api/v1/student/today")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        next_lesson_data = data.get("next_lesson")
        if next_lesson_data:
            self.assertNotEqual(next_lesson_data["id"], str(self.draft_lesson.id))

    def test_finish_endpoint_rejects_draft_lesson_with_404(self):
        resp = self.client.post(f"/api/v1/lessons/{self.draft_lesson.id}/finish")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "not_found")
        self.assertEqual(resp.json()["error"]["message"], "This lesson has no published content yet.")

    def test_course_completion_ignores_draft_lessons(self):
        published_lessons = list(planner.accessible_lessons(self.student, course_ids=[self.course.id]))
        for item in published_lessons:
            progress.mark_finished(self.student, item)

        skills = Skill.objects.filter(
            lesson_links__lesson__in=published_lessons, lesson_links__role="teaches"
        ).distinct()
        for skill in skills:
            MasteryState.objects.update_or_create(
                student=self.student,
                skill=skill,
                defaults={"level": MasteryState.Level.DEVELOPING, "score": 0.7, "evidence_count": 2},
            )

        completed = progress.check_course_completion(self.student, self.course)
        self.assertTrue(completed)
        self.assertTrue(CourseCompletion.objects.filter(student=self.student, course=self.course).exists())
