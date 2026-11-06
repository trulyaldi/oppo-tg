import pytest

from src.contacts.classify import classify_role


@pytest.mark.parametrize(
    "role,kind,conf",
    [
        ("Technical Recruiter", "recruiter", 0.9),
        ("Talent Acquisition Partner", "recruiter", 0.9),
        ("HR-менеджер", "recruiter", 0.9),
        ("Рекрутер", "recruiter", 0.9),
        ("Co-founder & CTO", "founder", 0.9),
        ("Основатель", "founder", 0.9),
        ("CEO", "founder", 0.6),
        ("Head of Quant Research", "hiring_manager", 0.8),
        ("Engineering Manager", "hiring_manager", 0.8),
        ("Manager", "hiring_manager", 0.6),
        ("Руководитель отдела данных", "hiring_manager", 0.8),
        ("Founding Engineer", "engineer", 0.8),
        ("Research Scientist", "engineer", 0.8),
        ("Разработчик", "engineer", 0.8),
        ("Data Analyst", "engineer", 0.6),
        ("Office coordinator", "other", 0.3),
        ("", "other", 0.3),
    ],
)
def test_table(role, kind, conf):
    assert classify_role(role) == (kind, conf)


def test_context_only_when_role_empty_and_capped():
    assert classify_role("", "our recruiter") == ("recruiter", 0.5)
    assert classify_role("Office coordinator", "our recruiter") == ("other", 0.3)
