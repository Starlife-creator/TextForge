import cn2an, re

def cn2num(s: str) -> int:
    if s.isdigit(): return int(s)
    s = s.replace('两', '二')
    try: return int(cn2an.cn2an(s, 'smart'))
    except Exception: return 0

def natural_sort_key(name: str) -> tuple:
    match = re.search(r'第?([零一二三四五六七八九十百千万亿0-9两]+)[章回节卷]?', name)
    return (cn2num(match.group(1)) if match else 0, name)
