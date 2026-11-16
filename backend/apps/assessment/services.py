from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from apps.accounts import permissions
from apps.core.errors import ApiError
from apps.learning import mastery, progress
from apps.operations import audit

from .models import Answer, Attempt, AttemptItem, Question, QuestionVersion

QUIZ_SIZE = 5


def published_question_version(question):
    return question.versions.filter(status=QuestionVersion.Status.PUBLISHED).first()


def published_quiz_versions(lesson):
    """The published version of each multiple-choice quiz question in a lesson, in question order. One query."""
    return (
        QuestionVersion.objects.filter(
            status=QuestionVersion.Status.PUBLISHED,
            question__lesson=lesson,
            question__purpose=Question.Purpose.QUIZ,
            question__type=Question.Type.MCQ,
        )
        .select_related("question")
        .order_by("question__position", "question__id")
    )


def next_question_version_no(question):
    return (question.versions.aggregate(m=Max("version_no"))["m"] or 0) + 1


@transaction.atomic
def publish_question_version(version, by_user):
    """Same rules as lesson content: approved first, and only a curriculum lead for the lesson (or super admin)."""
    question = version.question
    permissions.require_can_publish(by_user, question.lesson if question.lesson_id else None)
    if version.status != QuestionVersion.Status.APPROVED:
        raise ApiError(409, "conflict", "Only approved content can be published.")
    previous = published_question_version(version.question)
    if previous:
        previous.status = QuestionVersion.Status.ARCHIVED
        previous.save(update_fields=["status", "updated_at"])
    version.status = QuestionVersion.Status.PUBLISHED
    version.published_at = timezone.now()
    version.published_by = by_user
    version.save(update_fields=["status", "published_at", "published_by", "updated_at"])
    audit.record(by_user, "question.published", version)
    return version


@transaction.atomic
def start_lesson_quiz(student, lesson):
    """Resume an unfinished attempt, or start a new one from the lesson's published quiz questions."""
    open_attempt = Attempt.objects.filter(
        student=student, lesson=lesson, kind=Attempt.Kind.LESSON_QUIZ, submitted_at__isnull=True
    ).first()
    if open_attempt:
        return open_attempt
    versions = list(published_quiz_versions(lesson)[:QUIZ_SIZE])
    if not versions:
        raise ApiError(404, "not_found", "This lesson has no quiz yet.")
    try:
        with transaction.atomic():
            attempt = Attempt.objects.create(student=student, lesson=lesson, max_score=len(versions))
            AttemptItem.objects.bulk_create(
                [AttemptItem(attempt=attempt, question_version=v, position=i) for i, v in enumerate(versions, start=1)]
            )
            return attempt
    except IntegrityError:
        existing = Attempt.objects.filter(
            student=student, lesson=lesson, kind=Attempt.Kind.LESSON_QUIZ, submitted_at__isnull=True
        ).first()
        if existing:
            return existing
        raise


def _item(student, attempt_id, position):
    item = (
        AttemptItem.objects.select_related("attempt", "question_version__question__skill")
        .filter(attempt_id=attempt_id, attempt__student=student, position=position)
        .first()
    )
    if not item:
        raise ApiError(404, "not_found", "Question not found in this attempt.")
    if item.attempt.submitted_at:
        raise ApiError(409, "conflict", "This quiz is already submitted.")
    if hasattr(item, "answer"):
        raise ApiError(409, "conflict", "This question is already answered.")
    return item


def use_hint(student, attempt_id, position):
    item = _item(student, attempt_id, position)
    hints = item.question_version.body.get("hints", [])
    if item.hints_used >= len(hints):
        raise ApiError(409, "conflict", "No more hints for this question.")
    hint = hints[item.hints_used]
    item.hints_used += 1
    item.save(update_fields=["hints_used"])
    return hint, len(hints) - item.hints_used


@transaction.atomic
def answer_item(student, attempt_id, position, choice_index):
    item = _item(student, attempt_id, position)
    body = item.question_version.body
    options = body.get("options", [])
    if not 0 <= choice_index < len(options):
        raise ApiError(400, "validation_error", "Choose one of the options.", {"choice_index": "out of range"})
    correct = choice_index == body.get("answer_index")
    answer = Answer.objects.create(
        item=item,
        response={"choice_index": choice_index},
        is_correct=correct,
        marks=1 if correct else 0,
        marked_by=Answer.MarkedBy.CODE,
    )
    state = mastery.record_evidence(
        student, item.question_version.question.skill, mastery.credit_for(correct, item.hints_used), answer=answer
    )
    misconception = None if correct else (body.get("misconceptions") or {}).get(str(choice_index))
    return {
        "correct": correct,
        "correct_index": body.get("answer_index"),
        "explanation": body.get("explanation", ""),
        "misconception": misconception,
        "mastery": {"skill": state.skill.code, "level": state.level, "score": state.score},
    }


@transaction.atomic
def submit_attempt(student, attempt_id):
    attempt = Attempt.objects.select_related("lesson__module__course").filter(pk=attempt_id, student=student).first()
    if not attempt:
        raise ApiError(404, "not_found", "Attempt not found.")
    if attempt.submitted_at:
        raise ApiError(409, "conflict", "This quiz is already submitted.")
    answers = Answer.objects.filter(item__attempt=attempt).select_related("item__question_version__question__skill")
    attempt.score = sum(1 for a in answers if a.is_correct)
    attempt.submitted_at = timezone.now()
    attempt.save(update_fields=["score", "submitted_at"])

    from apps.learning.models import MasteryEvent  # local import keeps app loading order simple

    changes = {}
    for ev in MasteryEvent.objects.filter(answer__item__attempt=attempt).select_related("skill").order_by("created_at"):
        c = changes.setdefault(ev.skill.code, {"skill": ev.skill.code, "name": ev.skill.name, "before": ev.old_score})
        c["after"] = ev.new_score
    course_completed = False
    if attempt.lesson:
        progress.mark_finished(student, attempt.lesson)  # taking the quiz finishes the lesson
        course_completed = progress.check_course_completion(student, attempt.lesson.module.course)
    return attempt, list(changes.values()), course_completed
