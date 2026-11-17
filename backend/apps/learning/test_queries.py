"""Student learning endpoints run a fixed number of queries however much content or history exists (Q2)."""

from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

from apps.accounts.models import User
from apps.assessment.models import Question, QuestionVersion
from apps.catalogue.models import Lesson, Skill
from apps.commerce.models import Entitlement
from apps.core.testing import QueryBudgetMixin
from apps.learning import planner
from apps.learning.access import entitled_course_ids
from apps.learning.models import LessonProgress
from apps.learning.planner import streak_days

from .tests import LoopBase, post

WRONG = {"what-is-data": [0, 1, 0], "learning-from-examples": [0, 0, 0], "is-the-model-any-good": [0, 0, 0]}


class LearningQueryTests(QueryBudgetMixin, LoopBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.author = User.objects.create_user(email="qa-author@example.com", password="x-long-pass-1", full_name="A")
        cls.reviewer = User.objects.create_user(email="qa-rev@example.com", password="x-long-pass-1", full_name="R")

    def setUp(self):
        self.student, self.client = self.make_student()
        Entitlement.objects.create(student=self.student, product_type="course", product_id=self.course.id)

    def add_quiz_questions(self, lesson, n):
        skill = Skill.objects.get(code="AI-DATA-01")
        start = lesson.questions.count() + 1
        for position in range(start, start + n):
            question = Question.objects.create(lesson=lesson, skill=skill, position=position)
            QuestionVersion.objects.create(
                question=question,
                version_no=1,
                body={"stem": f"Extra {position}", "options": ["a", "b"], "answer_index": 0, "hints": []},
                status=QuestionVersion.Status.PUBLISHED,
                author=self.author,
                reviewer=self.reviewer,
            )

    def take_quiz(self, slug, client=None):
        client = client or self.client
        lesson = Lesson.objects.get(slug=slug)
        aid = post(client, f"/lessons/{lesson.id}/quiz/start").json()["attempt_id"]
        for position, choice in enumerate(WRONG[slug], start=1):
            post(client, f"/attempts/{aid}/answers", {"position": position, "choice_index": choice})
        post(client, f"/attempts/{aid}/submit")
        post(client, f"/lessons/{lesson.id}/finish")

    def test_open_lesson(self):
        self.assertQueriesDoNotGrow(
            lambda: self.client.get(f"/api/v1/lessons/{self.lesson1.id}"),
            lambda: self.add_quiz_questions(self.lesson1, 6),
            budget=12,
        )

    def test_resume_quiz(self):
        self.assertQueriesDoNotGrow(
            lambda: post(self.client, f"/lessons/{self.lesson1.id}/quiz/start"),
            lambda: self.add_quiz_questions(self.lesson1, 6),
            budget=12,
        )

    def test_new_quiz_attempt_does_not_query_per_question(self):
        first, res, _ = self.count_queries(lambda: post(self.client, f"/lessons/{self.lesson1.id}/quiz/start"))
        self.assertEqual(len(res.json()["items"]), 3)
        self.add_quiz_questions(self.lesson1, 6)
        _, other = self.make_student(email="other@example.com")
        second, res, _ = self.count_queries(lambda: post(other, f"/lessons/{self.lesson1.id}/quiz/start"))
        self.assertEqual(len(res.json()["items"]), 5)  # capped at QUIZ_SIZE
        self.assertEqual(first, second)

    def test_quiz_items_come_in_question_order(self):
        self.add_quiz_questions(self.lesson1, 2)  # positions 4 and 5
        items = post(self.client, f"/lessons/{self.lesson1.id}/quiz/start").json()["items"]
        self.assertEqual([i["position"] for i in items], [1, 2, 3, 4, 5])
        self.assertEqual([i["stem"] for i in items[3:]], ["Extra 4", "Extra 5"])

    def test_today(self):
        self.take_quiz("what-is-data")  # leaves a weak skill, so the review branch runs both times
        self.assertQueriesDoNotGrow(
            lambda: self.client.get("/api/v1/student/today"),
            lambda: self.take_quiz("learning-from-examples"),
            budget=16,
        )

    def test_today_calculates_entitlements_once(self):
        with patch(
            "apps.learning.api.entitled_course_ids",
            wraps=entitled_course_ids,
        ) as entitlement_lookup:
            response = self.client.get("/api/v1/student/today")

        self.assertEqual(response.status_code, 200, response.content)
        entitlement_lookup.assert_called_once_with(self.student)

    def test_shared_entitlements_reduce_planner_queries(self):
        self.take_quiz("what-is-data")

        def original_approach():
            planner.next_lesson(self.student)
            planner.review_item(self.student)

        def optimized_approach():
            course_ids = entitled_course_ids(self.student)
            planner.next_lesson(self.student, course_ids=course_ids)
            planner.review_item(self.student, course_ids=course_ids)

        original_count, _, _ = self.count_queries(original_approach)
        optimized_count, _, _ = self.count_queries(optimized_approach)

        self.assertLess(
            optimized_count,
            original_count,
            f"Expected fewer queries, but original={original_count}, optimized={optimized_count}",
        )

    def test_record(self):
        self.take_quiz("what-is-data")
        self.assertQueriesDoNotGrow(
            lambda: self.client.get("/api/v1/student/record"),
            lambda: self.take_quiz("learning-from-examples"),
            budget=10,
        )

    def test_me(self):
        self.assertQueriesDoNotGrow(
            lambda: self.client.get("/api/v1/me"), lambda: self.take_quiz("what-is-data"), budget=10
        )


class StreakTests(LoopBase):
    def finish_on(self, student, lesson, days_ago):
        LessonProgress.objects.create(
            student=student,
            lesson=lesson,
            status=LessonProgress.Status.FINISHED,
            finished_at=timezone.now() - timedelta(days=days_ago),
        )

    def test_streak_stops_at_the_first_gap(self):
        student, _ = self.make_student()
        lessons = list(Lesson.objects.filter(module__course=self.course, module__position=1))
        for lesson, days_ago in zip(lessons, [0, 1, 3], strict=True):  # today, yesterday, then a gap
            self.finish_on(student, lesson, days_ago)
        self.assertEqual(streak_days(student), 2)

    def test_no_activity_today_means_no_streak(self):
        student, _ = self.make_student()
        self.finish_on(student, self.lesson1, days_ago=1)
        self.assertEqual(streak_days(student), 0)
