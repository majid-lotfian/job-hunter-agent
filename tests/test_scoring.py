"""Regression tests for core.score_job (title + requirements + seniority/location/language)."""
import os
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.TemporaryDirectory()
os.environ.setdefault('DB_PATH', str(Path(_TMP.name) / 'scoring-test.sqlite3'))
os.environ.setdefault('DASHBOARD_TOKEN', 'test-token-for-unit-tests')

from app import core  # noqa: E402

SETTINGS = {'matching': {  # mirrors the repository search.yaml
    'titles_any': ['AI Engineer', 'Machine Learning Engineer', 'Applied AI', 'AI Scientist', 'LLM Engineer'],
    'keywords_any': ['LLM', 'agentic', 'RAG', 'fine-tuning', 'foundation model', 'Python'],
    'locations_any': ['Netherlands', 'Amsterdam', 'Remote', 'Europe'], 'excluded_terms': ['intern', 'internship'],
    'min_score': 25, 'max_age_days': 30}}
STRONG = ('You will design, train and deploy machine learning and deep learning models in Python with PyTorch, '
          'build RAG pipelines and LLM agents, fine-tune transformers, run model evaluation and MLOps for inference '
          'in production, and work with embeddings and vector search.')
WEAK = 'Join our friendly team. You will attend meetings, write documentation and support colleagues with Excel reports.'
TARGET_TITLES = ['Machine Learning Engineer', 'Applied Scientist', 'Research Engineer', 'Deep Learning Engineer', 'NLP Engineer',
                 'Foundation Model Engineer', 'LLM Engineer', 'Generative AI Engineer', 'Agentic AI Engineer', 'AI Platform Engineer',
                 'MLOps Engineer', 'AI Solutions Engineer', 'AI Research Scientist']


def score(title, description=STRONG, location='Amsterdam, Netherlands', **extra):
    return core.score_job({'title': title, 'description': description, 'location': location, **extra}, SETTINGS)


class ScoringTests(unittest.TestCase):
    def test_target_titles_with_relevant_requirements_reach_cv_threshold(self):
        for title in TARGET_TITLES:
            with self.subTest(title=title):
                self.assertGreaterEqual(score(f'Senior {title}'), 70)

    def test_title_alone_is_not_enough(self):
        for title in TARGET_TITLES:
            with self.subTest(title=title):
                self.assertLess(score(f'Senior {title}', WEAK), 70)

    def test_irrelevant_roles_stay_low(self):
        self.assertEqual(score('Account Executive', WEAK), -1)                 # no title or keyword match
        self.assertLess(score('Account Executive', STRONG), 70)                # AI words in the text, wrong role
        self.assertLess(score('Backend Developer', 'Java Spring services; some Python scripts.'), 70)
        self.assertEqual(score('Machine Learning Intern'), -1)                 # excluded term
        self.assertEqual(score('Senior AI Engineer', location='Berlin, Germany'), -1)  # location gate unchanged

    def test_seniority_location_and_language_adjust(self):
        base = score('Machine Learning Engineer', STRONG[:160])
        self.assertGreater(score('Senior Machine Learning Engineer', STRONG[:160]), base)
        self.assertGreater(base, score('Machine Learning Engineer', STRONG[:160], location='Remote - Europe'))
        dutch = STRONG[:160] + ' Fluent in Dutch is required.'
        self.assertEqual(score('Machine Learning Engineer', dutch), base - 10)
        self.assertEqual(score('Machine Learning Engineer', STRONG[:160], language_req='dutch_required'), base - 10)

    def test_breakdown_is_transparent_and_bounded(self):
        value, parts = core.score_breakdown({'title': 'Staff ML Engineer', 'description': STRONG * 3,
                                             'location': 'Amsterdam'}, SETTINGS)
        self.assertLessEqual(value, 100)
        self.assertEqual(parts['title_kind'], 'primary')
        self.assertLessEqual(parts['requirements'], core.SCORE_WEIGHTS['requirements_cap'])
        self.assertEqual(value, parts['title'] + parts['requirements'] + parts['seniority'] + parts['location'] + parts['language'])
        self.assertEqual(core.score_breakdown({'title': 'Chef', 'description': '', 'location': 'Amsterdam'}, SETTINGS)[1]['gate'],
                         'no title or keyword match')


if __name__ == '__main__':
    unittest.main()
