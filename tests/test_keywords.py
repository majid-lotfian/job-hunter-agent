"""Strong AI keyword priority in core.score_breakdown."""
import os
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.TemporaryDirectory()
os.environ.setdefault('DB_PATH', str(Path(_TMP.name) / 'kw-test.sqlite3'))
os.environ.setdefault('DASHBOARD_TOKEN', 'test-token-for-unit-tests')

from app import core  # noqa: E402

SETTINGS = {'matching': {'titles_any': ['AI Engineer', 'Machine Learning Engineer'], 'keywords_any': ['Python'],
                         'locations_any': ['Netherlands', 'Amsterdam', 'Remote', 'Europe'],
                         'excluded_terms': ['intern', 'internship'], 'max_age_days': 30}}
THIN = 'Python services for our platform team.'


def score(title, description, location='Amsterdam, Netherlands', **extra):
    return core.score_breakdown({'title': title, 'description': description, 'location': location, **extra}, SETTINGS)


class KeywordMatchingTests(unittest.TestCase):
    def fams(self, text):
        return set(core.strong_ai_keywords(text))

    def test_case_insensitive_and_variants(self):
        cases = {
            'We build rag systems': {'RAG'}, 'RAG pipelines': {'RAG'}, 'Retrieval-Augmented Generation': {'RAG'},
            'retrieval augmented generation': {'RAG'}, 'an AI agent': {'agent'}, 'AI Agents platform': {'agent'},
            'agent-based simulation': {'agent'}, 'Multi-Agent systems': {'agent'}, 'autonomous agents': {'agent'},
            'Agentic workflows': {'agentic'}, 'AGENTIC': {'agentic'}, 'LLM-based tools': {'LLM'}, 'LLMs': {'LLM'},
            'large language models': {'LLM'}, 'Large Language Model': {'LLM'}, 'fine-tuning': {'fine-tuning'},
            'fine tuned models': {'fine-tuning'}, 'finetuned': {'fine-tuning'}, 'Fine-Tune': {'fine-tuning'},
            'finetune': {'fine-tuning'}, 'FINE TUNING': {'fine-tuning'},
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.fams(text), expected)

    def test_word_boundaries_avoid_accidental_substrings(self):
        for text in ['drag and drop', 'storage and garage', 'reagents in the lab', 'management', 'agentur',
                     'fragment', 'fine dining', 'tuned in', 'pragmatic', 'allm', 'llmx', 'Agenda']:
            with self.subTest(text=text):
                self.assertEqual(self.fams(text), set())

    def test_non_ai_meanings_are_ignored(self):
        for text in ['Travel agent wanted', 'Real estate agents', 'customer service agent', 'insurance agent',
                     'RAG status reporting', 'red/amber/green RAG rating', 'LL.M. degree', 'LLM in law', 'Master of Laws']:
            with self.subTest(text=text):
                self.assertEqual(self.fams(text), set())

    def test_breakdown_exposes_matches(self):
        _, parts = score('AI Engineer', 'Build AI agents and RAG with LLMs; fine-tune models.')
        self.assertEqual(set(parts['ai_keywords']), {'agent', 'RAG', 'LLM', 'fine-tuning'})
        self.assertIn('ai agents', parts['ai_keywords']['agent'])


class KeywordPriorityTests(unittest.TestCase):
    def test_single_keyword_lifts_relevant_role_to_70(self):
        for kw in ['RAG', 'agent', 'agentic', 'LLM', 'fine-tune', 'finetune']:
            with self.subTest(kw=kw):
                value, parts = score('Machine Learning Engineer', f'{THIN} Experience with {kw} is key.')
                self.assertGreaterEqual(value, 70)
                self.assertEqual(parts['keyword_floor'], 70)

    def test_multiple_keywords_raise_priority_within_bounds(self):
        one = score('AI Engineer', f'{THIN} LLM work.')[0]
        three = score('AI Engineer', f'{THIN} LLM, RAG and agentic systems.')[0]
        five = score('AI Engineer', f'{THIN} LLM, RAG, agentic, AI agents, fine-tuning.' * 20)[0]
        self.assertLess(one, three)
        self.assertLessEqual(five, 100)
        self.assertGreaterEqual(five, 90)

    def test_unusual_title_needs_the_keyword_in_the_title(self):
        desc = 'Build RAG pipelines in Python with PyTorch, LLM evaluation, embeddings, transformers, vector search and MLOps.'
        self.assertLess(score('Software Engineer', desc)[0], 70)                       # boilerplate-prone: no floor
        self.assertGreaterEqual(score('Software Engineer, LLM Agents', desc)[0], 70)  # keyword in the title

    def test_not_applied_to_unsuitable_roles(self):
        self.assertLess(score('Data Engineer', 'Python and Airflow. Experience with LLMs is a plus.')[0], 70)  # not an AI role
        self.assertLess(score('Account Executive', 'Sell our LLM agents; Python helps.')[0], 70)               # sales
        self.assertEqual(score('Machine Learning Intern', 'Fine-tune LLMs')[0], -1)                            # internship
        self.assertEqual(score('AI Engineer', 'Build LLM agents', location='San Francisco, CA')[0], -1)         # geography
        dutch = score('AI Engineer', 'Build LLM agents. Fluent in Dutch is required.')
        self.assertLess(dutch[0], 70)                                                                          # Dutch required
        self.assertEqual(dutch[1]['keyword_floor'], 0)
