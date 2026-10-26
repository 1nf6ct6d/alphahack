"""Правила контрактов BCE v0.2 (собственное предложение). Используются только тестами контрактов,
это не реализация движка."""
LEVELS = ["low", "medium", "high"]
SUPPORT_TO_LEVEL = {"weak": "low", "moderate": "medium", "strong": "high"}
CAP_MEDIUM = {"source_conflict", "sector_generalization", "dictionary_unconfirmed",
              "unit_unknown", "currency_unknown", "low_coverage"}
STRENGTH_SCORE = {"low": 0.33, "medium": 0.66, "high": 1.0}
CONF_SCORE = {"low": 0.25, "medium": 0.55, "high": 0.8}


def lmin(*xs):
    return min(xs, key=LEVELS.index)


def hypothesis_confidence(data_reliability, inference_support, uncertainty_sources):
    c = lmin(data_reliability, SUPPORT_TO_LEVEL[inference_support])
    if CAP_MEDIUM & set(uncertainty_sources):
        c = lmin(c, "medium")
    return c


def sensitivity_confidence(basis_confidences, data_reliability, uncertainty_sources):
    c = lmin(data_reliability, *basis_confidences)
    if CAP_MEDIUM & set(uncertainty_sources):
        c = lmin(c, "medium")
    return c


def weight(strength, overall_confidence):
    return round(STRENGTH_SCORE[strength] * CONF_SCORE[overall_confidence], 2)


def usability(industry_status, groups_count):
    if industry_status in ("missing", "invalid", "name_only_unmapped"):
        return "unusable"
    if groups_count == 0:
        return "sector_only"
    if industry_status == "ambiguous":
        return "limited"
    return "full" if groups_count >= 3 else "limited"
