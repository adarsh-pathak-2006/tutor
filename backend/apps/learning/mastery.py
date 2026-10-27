"""Mastery from evidence (decisions C2, C3). One function to swap for a better model later."""

from django.db import transaction

from .models import MasteryEvent, MasteryState

ALPHA = 0.35  # weight of the newest answer
HINT_PENALTY = 0.25  # credit lost per hint used
MASTERED_SCORE = 0.75
MASTERED_MIN_EVIDENCE = 3
WEAK_SCORE = 0.40


def level_for(score: float, evidence: int) -> str:
    if evidence == 0:
        return MasteryState.Level.NOT_STARTED
    if score >= MASTERED_SCORE and evidence >= MASTERED_MIN_EVIDENCE:
        return MasteryState.Level.MASTERED
    if score < WEAK_SCORE:
        return MasteryState.Level.WEAK
    return MasteryState.Level.DEVELOPING


def credit_for(correct: bool, hints_used: int) -> float:
    return max(0.0, 1.0 - HINT_PENALTY * hints_used) if correct else 0.0


@transaction.atomic
def record_evidence(student, skill, credit: float, answer=None, source="quiz") -> MasteryState:
    state, _ = MasteryState.objects.select_for_update().get_or_create(student=student, skill=skill)
    old = state.score
    if state.evidence_count == 0:
        state.score = round(credit, 4)
    else:
        state.score = round(old + ALPHA * (credit - old), 4)
    state.evidence_count += 1
    state.level = level_for(state.score, state.evidence_count)
    state.save()
    MasteryEvent.objects.create(
        student=student, skill=skill, answer=answer, source=source, old_score=old, new_score=state.score
    )
    return state
