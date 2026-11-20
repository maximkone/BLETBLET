"""Wake-word matching. Recording is stopped only by confirmed silence."""
import re


def normalize(text):
    return ' '.join(re.findall(r'[а-яa-z0-9]+', str(text).lower().replace('ё', 'е')))


def phrase_words(phrase):
    return normalize(phrase).split()


def contains_phrase(target, words):
    return bool(target) and any(words[i:i + len(target)] == target
                               for i in range(len(words) - len(target) + 1))


def command_for(text, settings, recording=False, final=False):
    if recording:
        return None
    words = normalize(text).split()
    return 'start' if any(contains_phrase(phrase_words(settings[key]), words)
                          for key in ('start1', 'start2')) else None


def grammar_phrases(settings):
    return list(dict.fromkeys(normalize(settings[key])
                             for key in ('start1', 'start2')
                             if normalize(settings[key]))) + ['[unk]']
