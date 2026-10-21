"""Arabic presentation helpers; never rewrite identifiers, URLs or audit bodies."""
import re

RLM = '\u200f'
LRI = '\u2066'
PDI = '\u2069'
DIVIDER = '────────────────'


def rtl_html(text):
    """Set the paragraph direction and isolate code without changing copied digits."""
    text = re.sub(r'(?<!\u2066)(<code>.*?</code>)(?!\u2069)', LRI+r'\1'+PDI, text, flags=re.S)
    output = []
    line_start = True
    for part in re.split(r'(<code>.*?</code>|<pre>.*?</pre>)', text, flags=re.S):
        if part.startswith(('<code>', '<pre>')):
            if line_start:output.append(RLM)
            output.append(part)
            line_start = part.endswith('\n')
            continue
        for line in part.splitlines(keepends=True):
            if line_start and line.strip() and not line.startswith(RLM):output.append(RLM)
            output.append(line)
            line_start = line.endswith('\n')
    return ''.join(output)


def audit_body_html(body):
    """A readable view of the immutable original; stored/sent audit text is untouched."""
    from html import escape
    text = escape(body)
    return rtl_html(re.sub(r'(?<![\w])Telegram(?![\w])', 'تيليجرام', text))
