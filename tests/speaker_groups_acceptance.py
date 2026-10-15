"""Real CPU grouping using two different NPK-generated preset fixtures."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / 'tests/speaker-groups-results'
OUT.mkdir(exist_ok=True)


def main():
    with tempfile.TemporaryDirectory(prefix='npk-speaker-acceptance-', dir=ROOT / 'tests') as isolated:
        os.environ['VIETDUB_DATA_DIR'] = isolated
        from speaker_groups import analyze_speakers
        fixtures = ROOT / 'tests/vieneu-independent-results'
        inputs = [fixtures / 'NPK-Hai-Dang.wav', fixtures / 'NPK-text-to-speech.wav', fixtures / 'NPK-Hai-Dang.wav']
        segments, cursor = [], 0
        source = OUT / 'two-presets.wav'
        with wave.open(str(source), 'wb') as destination:
            destination.setnchannels(1)
            destination.setsampwidth(2)
            destination.setframerate(48000)
            for index, path in enumerate(inputs):
                with wave.open(str(path), 'rb') as audio:
                    assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (48000, 1, 2)
                    frames = min(audio.getnframes(), 4 * 48000)
                    destination.writeframes(audio.readframes(frames))
                    segments.append(dict(start=cursor / 48000, end=(cursor + frames) / 48000,
                                         text=f'Fixture {index + 1}', voice_id='unchanged'))
                    cursor += frames
                    destination.writeframes(b'\0' * 24000)
                    cursor += 12000
        project = dict(media={'path': str(source)}, segments=segments, params={'workload_mode': 'quiet'})
        events, began = [], time.perf_counter()
        result = analyze_speakers(project, OUT, progress=events.append)
        grouping = [row['group_id'] for row in result['suggestions']]
        assert len(grouping) == 3 and grouping[0] == grouping[2] and grouping[0] != grouping[1], grouping
        assert all(row['voice_id'] == 'unchanged' for row in project['segments'])
        report = dict(ok=True, backend='cpu', fixture_presets=['Hải Đăng', 'Thùy Dung', 'Hải Đăng'],
                      observed_grouping=grouping, elapsed_seconds=time.perf_counter() - began,
                      progress_events=len(events), warnings=result['warnings'])
        (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
