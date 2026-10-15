"""ASS word highlighting only when recognized word timing matches displayed text."""
import math
import re
import unicodedata


def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold() if c.isalnum())


def timed_caption(row, displayed):
    words = row.get('words') or []
    tokens = re.findall(r'\S+|\s+', displayed)
    visible = [x for x in tokens if not x.isspace()]
    if not words or len(visible) != len(words):
        return None
    if any(normalized(token) != normalized(str(word.get('word', ''))) for token,word in zip(visible,words)):
        return None
    cursor = float(row['start'])
    events, index = [], 0
    for token in tokens:
        if token.isspace():
            events.append(token.replace('\n', r'\N'))
            continue
        word = words[index]; index += 1
        start, end = float(word['start']), float(word['end'])
        if not math.isfinite(start + end) or start < cursor - .01 or end < start or end > float(row['end']) + .01:
            return None
        if start > cursor + .01:
            events.append(r'{\k' + str(max(1, round((start-cursor)*100))) + '}')
        events.append(r'{\kf' + str(max(1, round((end-start)*100))) + '}' + token.replace('{', '｛').replace('}', '｝'))
        cursor = end
    return ''.join(events)


def estimated_caption(row, displayed):
    """Estimate word sweeps within a cue when exact word alignment is absent.

    These durations are presentation estimates, never recognition timestamps.
    Each visible token receives at least one centisecond, the integer durations
    sum to the cue duration, and line breaks/words are preserved. Extremely short
    cues that cannot give each word a centisecond remain a static caption.
    """
    try:
        start, end = float(row['start']), float(row['end'])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(start + end) or end <= start:
        return None
    tokens = re.findall(r'\S+|\s+', displayed)
    words = [token for token in tokens if not token.isspace()]
    total = round((end - start) * 100)
    if not words or total < len(words):
        return None
    weights = [max(1, len(normalized(word))) for word in words]
    remaining, weight_sum = total - len(words), sum(weights)
    numerators = [remaining * weight for weight in weights]
    durations = [1 + numerator // weight_sum for numerator in numerators]
    leftover = total - sum(durations)
    order = sorted(range(len(words)), key=lambda index: (-(numerators[index] % weight_sum), index))
    for index in order[:leftover]:
        durations[index] += 1
    events, index = [], 0
    for token in tokens:
        if token.isspace():
            events.append(token.replace('\n', r'\N'))
        else:
            events.append(r'{\kf' + str(durations[index]) + '}' +
                          token.replace('{', '｛').replace('}', '｝'))
            index += 1
    return ''.join(events)
