"""Questions, attempts and answers (DATA_MODEL.md §7).

Decision C10: question text is versioned in QuestionVersion (same rules as lesson ContentVersion: published
versions are immutable, one published version per question, reviewer ≠ author). Keeping it in this app avoids a
circular dependency with `content`.

Question body (JSON):
{"stem": "…", "options": ["A", "B", "C", "D"], "answer_index": 1, "explanation": "…",
 "hints": ["first hint", "second hint"], "misconceptions": {"0": "note shown if option 0 is chosen"}}
"""

import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q


class Question(models.Model):
    class Type(models.TextChoices):
        MCQ = "mcq", "Multiple choice"
        SHORT_ANSWER = "short_answer", "Short answer (AI-marked, later)"

    class Purpose(models.TextChoices):
        QUIZ = "quiz", "Lesson quiz"
        PRACTICE = "practice", "Practice"
        WARMUP = "warmup", "Warm-up check"
        EXPLORATORY = "exploratory", "Exploratory test"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lesson = models.ForeignKey(
        "catalogue.Lesson", on_delete=models.CASCADE, null=True, blank=True, related_name="questions"
    )
    skill = models.ForeignKey("catalogue.Skill", on_delete=models.PROTECT, related_name="questions")
    type = models.CharField(max_length=15, choices=Type.choices, default=Type.MCQ)
    difficulty = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1), MaxValueValidator(3)])
    purpose = models.CharField(max_length=12, choices=Purpose.choices, default=Purpose.QUIZ)
    position = models.PositiveSmallIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["lesson", "position"]

    def __str__(self):
        v = self.versions.filter(status="published").first()
        stem = (v.body.get("stem", "") if v else "")[:60]
        return f"[{self.skill.code}] {stem or self.id}"


class QuestionVersion(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        IN_REVIEW = "in_review", "In review"
        CHANGES_REQUESTED = "changes_requested", "Changes requested"
        APPROVED = "approved", "Approved"
        PUBLISHED = "published", "Published"
        ARCHIVED = "archived", "Archived"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="versions")
    version_no = models.PositiveIntegerField()
    body = models.JSONField(default=dict)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    is_ai_draft = models.BooleanField(default=False)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-version_no"]
        constraints = [
            models.UniqueConstraint(fields=["question", "version_no"], name="unique_question_version_no"),
            models.UniqueConstraint(
                fields=["question"], condition=Q(status="published"), name="one_published_version_per_question"
            ),
            models.CheckConstraint(
                condition=Q(reviewer__isnull=True) | ~Q(reviewer=models.F("author")),
                name="question_reviewer_is_not_author",
            ),
        ]

    def __str__(self):
        return f"{self.body.get('stem', '')[:50]} v{self.version_no} ({self.status})"


class Attempt(models.Model):
    class Kind(models.TextChoices):
        LESSON_QUIZ = "lesson_quiz", "Lesson quiz"
        WARMUP = "warmup", "Warm-up check"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="attempts")
    kind = models.CharField(max_length=15, choices=Kind.choices, default=Kind.LESSON_QUIZ)
    lesson = models.ForeignKey("catalogue.Lesson", on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    started_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    score = models.PositiveSmallIntegerField(null=True, blank=True)
    max_score = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["student", "lesson", "kind"],
                condition=Q(submitted_at__isnull=True),
                name="unique_open_attempt_per_student_lesson_kind",
            ),
        ]

    def __str__(self):
        return f"{self.student} · {self.lesson} · {self.score}/{self.max_score}"


class AttemptItem(models.Model):
    """One question as shown in one attempt — fixed to the exact version the student saw (M6).
    Decision C11: attempts hold their own item list instead of a separate Quiz table in v1."""

    attempt = models.ForeignKey(Attempt, on_delete=models.CASCADE, related_name="items")
    question_version = models.ForeignKey(QuestionVersion, on_delete=models.PROTECT, related_name="+")
    position = models.PositiveSmallIntegerField()
    hints_used = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["attempt", "position"]
        constraints = [models.UniqueConstraint(fields=["attempt", "position"], name="unique_item_position")]

    def __str__(self):
        return f"{self.attempt_id} #{self.position}"


class Answer(models.Model):
    class MarkedBy(models.TextChoices):
        CODE = "code", "Code"
        AI = "ai", "AI"
        TEACHER = "teacher", "Teacher"

    item = models.OneToOneField(AttemptItem, on_delete=models.CASCADE, related_name="answer")
    response = models.JSONField(default=dict)  # {"choice_index": 2}
    is_correct = models.BooleanField(null=True)
    marks = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    marked_by = models.CharField(max_length=10, choices=MarkedBy.choices, default=MarkedBy.CODE)
    feedback = models.TextField(blank=True)
    answered_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Answer to {self.item} ({'correct' if self.is_correct else 'wrong'})"
