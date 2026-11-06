"""Recompute score + matched AI keywords for stored jobs with the current scoring rules.

Only `score` and `ai_keywords` change. Job statuses, approvals, applications and CVs are never touched; the CV
worker's backfill will queue newly eligible (>= CV_MIN_SCORE) active jobs as usual.

    .venv/bin/python -m app.rescore --dry-run   # show what would change
    .venv/bin/python -m app.rescore             # apply
"""
import argparse
import collections

from .core import config, connect, init_db, score_breakdown
from .workflow import workflow_for


def rescore(dry_run=False):
    settings = config('search.yaml')
    changes, tiers_before, tiers_after = [], collections.Counter(), collections.Counter()
    with connect() as db:
        rows = db.execute("SELECT id, title, description, location, location_label, location_region, language_req, score, ai_keywords "
                          "FROM jobs WHERE COALESCE(availability,'active')='active'").fetchall()
    for r in rows:
        new, parts = score_breakdown({'title': r['title'], 'description': r['description'] or '',
                                      'location': r['location_label'] or r['location'] or '',
                                      'location_region': r['location_region'], 'language_req': r['language_req']}, settings)
        new = max(new, 0)
        keywords = ','.join(parts.get('ai_keywords') or {}) or None
        tiers_before[workflow_for(r['score'])] += 1
        tiers_after[workflow_for(new)] += 1
        if new != r['score'] or keywords != r['ai_keywords']:
            changes.append((r['id'], new, keywords))
    if not dry_run and changes:
        with connect() as db:
            db.executemany('UPDATE jobs SET score=?, ai_keywords=? WHERE id=?', [(s, k, i) for i, s, k in changes])
    return {'jobs': len(rows), 'changed': len(changes), 'tiers_before': dict(tiers_before), 'tiers_after': dict(tiers_after),
            'applied': not dry_run}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m app.rescore', description=__doc__.splitlines()[0])
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    init_db()
    print(rescore(dry_run=args.dry_run))


if __name__ == '__main__':
    main()
