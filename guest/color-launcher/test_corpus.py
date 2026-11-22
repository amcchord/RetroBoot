"""Game-link planning checks; no guest media or toolchain needed."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
module = importlib.util.spec_from_file_location('build_corpus', HERE / 'build_corpus.py')
corpus = importlib.util.module_from_spec(module)
module.loader.exec_module(corpus)
from rbapps.tree import TreeError


class CorpusPlanTests(unittest.TestCase):
    def setUp(self):
        self.roster = json.loads((HERE.parents[1] /
                                 'apps/manifests/68k-games-utilities-v1.json').read_text())

    def test_common_roster_has_unique_project_links_and_qualified_depths(self):
        links = corpus.plan(self.roster)
        self.assertEqual(len(links), 23)
        self.assertEqual(sum(item['depth'] == 8 for item in links), 22)
        self.assertEqual(sum(item['depth'] == 4 for item in links), 1)
        self.assertEqual({item['creator'] for item in links}, {f'R{i}' for i in range(200, 223)})
        self.assertEqual(len({tuple(item['helper']) for item in links}), 23)
        self.assertNotIn('monkey-island', [item['id'] for item in links])

    def test_loom_configuration_opens_original_without_fixed_return_depth(self):
        loom = next(item for item in corpus.plan(self.roster) if item['id'] == 'loom')
        self.assertEqual(loom['original'], ['Games', 'Loom', 'Loom™'])
        self.assertEqual(loom['helper'], ['Games', 'Loom', 'Play Loom'])
        data = corpus.config_bytes(loom)
        self.assertEqual(data[:3], bytes([1, 4, 5]))
        self.assertEqual(data[3:].decode('mac_roman'), 'Loom™')
        # No fixed desktop depth is encoded. color.c captures the actual PixMap.
        self.assertEqual(len(data), 3 + data[2])

    def test_case_insensitive_helper_collision_and_duplicate_id_fail(self):
        for field in ('id', 'title'):
            roster = copy.deepcopy(self.roster)
            other = copy.deepcopy(roster['games'][0])
            other['id'] = 'second-id'
            other[field] = roster['games'][0][field].swapcase() if field == 'title' else other[field]
            if field == 'id':
                other['id'] = roster['games'][0]['id']
                other['title'] = 'Second Title'
            roster['games'].append(other)
            with self.subTest(field=field), self.assertRaises(ValueError):
                corpus.plan(roster)

    def test_invalid_hfs_target_or_helper_is_rejected(self):
        for name in ('..', 'Bad:Name', 'Bad\0Name', 'x' * 32):
            roster = copy.deepcopy(self.roster)
            roster['games'][0]['installed_app'][-1] = name
            with self.subTest(original=name), self.assertRaises((ValueError, TreeError)):
                corpus.plan(roster)
        roster = copy.deepcopy(self.roster)
        roster['games'][0]['title'] = 'x' * 30  # "Play " would exceed the HFS limit.
        with self.assertRaises((ValueError, TreeError)):
            corpus.plan(roster)

    def test_unqualified_depth_and_non_sibling_original_are_rejected(self):
        for edit in ({'required_depth': 16}, {'installed_app': ['Other', 'App', 'App']},
                     {'installed_app': ['Games', 'Nested', 'Folder', 'App']}):
            roster = copy.deepcopy(self.roster)
            roster['games'][0].update(edit)
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                corpus.plan(roster)


if __name__ == '__main__':
    unittest.main()
