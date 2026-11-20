import tempfile
from pathlib import Path
import unittest

import command_rules as rules
from wake import Settings
from voice_engine import Engine


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(Path(self.temp.name) / 'settings.json')
        self.engine = Engine(self.settings, lambda *_: None)
        self.addCleanup(self.temp.cleanup)

    def test_start_matches_inside_utterance(self):
        self.assertEqual(rules.command_for('Алло!', self.settings), 'start')
        self.assertEqual(rules.command_for('слышь', self.settings), 'start')
        self.assertEqual(rules.command_for('ну алло', self.settings), 'start')
        self.assertEqual(rules.command_for('так алло начинаем', self.settings), 'start')
        self.assertIsNone(rules.command_for('аллон', self.settings))
        self.assertIsNone(rules.command_for('слово', self.settings))
        # Once dictation is running no recognised word may stop or restart it.
        self.assertIsNone(rules.command_for('алло', self.settings, recording=True))

    def test_no_words_stop_recording(self):
        for text in ('всё', 'все отправить', 'текст всё', 'текст все отправить', 'еще'):
            self.assertIsNone(rules.command_for(text, self.settings, recording=True, final=True), text)

    def test_two_word_start(self):
        self.settings.data['start1'] = 'эй компьютер'
        self.assertEqual(rules.command_for('эй компьютер', self.settings), 'start')
        self.assertIsNone(rules.command_for('эй', self.settings))

    def test_silence_minimum(self):
        self.settings.data['silence_seconds'] = 1
        with self.assertRaises(ValueError):
            Settings.validate(self.settings.data)
        self.settings.data['silence_seconds'] = 3
        self.assertEqual(Settings.validate(self.settings.data)['silence_seconds'], 3.0)

    def test_old_phrase_settings_are_ignored(self):
        path = Path(self.temp.name) / 'old.json'
        path.write_text('{"start1":"","start2":"слышь","end_word":"все","silence_stop":true}', encoding='utf-8')
        data = Settings(path).data
        self.assertNotIn('end_word', data)
        self.assertNotIn('silence_stop', data)
        self.assertEqual(data['start2'], 'слышь')

    def test_settings_round_trip(self):
        self.settings.data['start1'] = ''
        self.settings.save()
        reloaded = Settings(self.settings.path)
        self.assertEqual(reloaded['start1'], '')
        self.assertEqual(reloaded['start2'], 'слышь')

    def test_engine_silence_only_never_keyword(self):
        self.engine.stop_flag.clear()
        self.engine.apply_observation({'state': 'recording', 'hwnd': 2, 'signature': 'a'})
        self.assertIsNone(rules.command_for('все отправить', self.settings, recording=True, final=True))
        self.assertTrue(self.engine.request('send', 'silence'))
        self.assertEqual(self.engine.actions.get_nowait()[1], 'silence')

    def test_pause_blocks_actions(self):
        self.assertFalse(self.engine.request('send', 'keyword'))

    def test_one_action_at_time(self):
        self.engine.stop_flag.clear()
        self.assertTrue(self.engine.request('start', 'keyword'))
        self.assertFalse(self.engine.request('start', 'keyword'))

    def test_manual_recording_sync(self):
        self.engine.stop_flag.clear()
        self.engine.apply_observation({'state': 'recording', 'hwnd': 2, 'signature': 'a'})
        self.assertEqual(self.engine.state, 'dictating')
        self.engine.apply_observation({'state': 'idle', 'hwnd': 2})
        self.assertEqual(self.engine.state, 'idle')

    def test_settings_dictionary(self):
        self.assertEqual(rules.grammar_phrases(self.settings), ['алло', 'слышь', '[unk]'])


if __name__ == '__main__':
    unittest.main()
