"""Explicit subtitle line breaks; never discard words to meet a line budget."""
import textwrap


def wrap_caption(text, params):
    width = max(10, int(params.get('subtitle_line_chars',42)))
    return '\n'.join(line for paragraph in text.splitlines() for line in
        (textwrap.wrap(paragraph, width, break_long_words=True, break_on_hyphens=False) or ['']))


def output_dimensions(media, params):
    width, height = int(media['width']), int(media['height'])
    setting = params.get('export_resolution','source')
    if setting != 'source' and min(width,height) > int(setting):
        ratio = int(setting) / min(width,height)
        return max(2,round(width*ratio/2)*2), max(2,round(height*ratio/2)*2)
    return (width+1)//2*2,(height+1)//2*2
