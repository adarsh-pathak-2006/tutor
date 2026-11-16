from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory, TestCase

from apps.accounts.models import User
from apps.assessment.admin import QuestionVersionAdmin, QuestionVersionInline
from apps.assessment.models import Question, QuestionVersion


class QuestionImmutabilityTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.site = AdminSite()
        self.admin = QuestionVersionAdmin(QuestionVersion, self.site)
        self.inline = QuestionVersionInline(Question, self.site)
        self.request = self.factory.get("/admin/assessment/questionversion/")
        self.admin_user = User.objects.create_superuser(
            email="immutability_admin@example.com",
            password="Admin-Pass-2026",
            full_name="Immutability Admin",
        )
        self.request.user = self.admin_user

    def test_question_version_admin_readonly_fields_published(self):
        qv = QuestionVersion(status=QuestionVersion.Status.PUBLISHED)
        readonly = self.admin.get_readonly_fields(self.request, qv)
        self.assertIn("body", readonly)
        self.assertIn("question", readonly)
        self.assertIn("version_no", readonly)
        self.assertIn("is_ai_draft", readonly)
        self.assertIn("published_at", readonly)
        self.assertIn("published_by", readonly)

    def test_question_version_admin_readonly_fields_archived(self):
        qv = QuestionVersion(status=QuestionVersion.Status.ARCHIVED)
        readonly = self.admin.get_readonly_fields(self.request, qv)
        self.assertIn("body", readonly)
        self.assertIn("question", readonly)
        self.assertIn("version_no", readonly)
        self.assertIn("is_ai_draft", readonly)

    def test_question_version_admin_readonly_fields_draft(self):
        qv = QuestionVersion(status=QuestionVersion.Status.DRAFT)
        readonly = self.admin.get_readonly_fields(self.request, qv)
        self.assertNotIn("body", readonly)
        self.assertNotIn("question", readonly)
        self.assertNotIn("version_no", readonly)
        self.assertNotIn("is_ai_draft", readonly)
        self.assertIn("published_at", readonly)

    def test_question_version_admin_has_delete_permission(self):
        published_qv = QuestionVersion(status=QuestionVersion.Status.PUBLISHED)
        self.assertFalse(self.admin.has_delete_permission(self.request, published_qv))

        archived_qv = QuestionVersion(status=QuestionVersion.Status.ARCHIVED)
        self.assertFalse(self.admin.has_delete_permission(self.request, archived_qv))

        draft_qv = QuestionVersion(status=QuestionVersion.Status.DRAFT)
        self.assertTrue(self.admin.has_delete_permission(self.request, draft_qv))

    def test_question_version_inline_readonly_fields_published(self):
        qv = QuestionVersion(status=QuestionVersion.Status.PUBLISHED)
        readonly = self.inline.get_readonly_fields(self.request, qv)
        self.assertIn("body", readonly)
        self.assertIn("version_no", readonly)
        self.assertIn("is_ai_draft", readonly)

    def test_question_version_inline_readonly_fields_draft(self):
        qv = QuestionVersion(status=QuestionVersion.Status.DRAFT)
        readonly = self.inline.get_readonly_fields(self.request, qv)
        self.assertNotIn("body", readonly)
        self.assertNotIn("version_no", readonly)
        self.assertNotIn("is_ai_draft", readonly)

    def test_question_version_inline_has_delete_permission(self):
        published_qv = QuestionVersion(status=QuestionVersion.Status.PUBLISHED)
        self.assertFalse(self.inline.has_delete_permission(self.request, published_qv))

        archived_qv = QuestionVersion(status=QuestionVersion.Status.ARCHIVED)
        self.assertFalse(self.inline.has_delete_permission(self.request, archived_qv))

        draft_qv = QuestionVersion(status=QuestionVersion.Status.DRAFT)
        self.assertTrue(self.inline.has_delete_permission(self.request, draft_qv))
