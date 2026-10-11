import cn2an, re

_NUM = r'[零一二三四五六七八九十百千万亿0-9两]+'

def cn2num(s: str) -> int:
    if s.isdigit(): return int(s)
    s = s.replace('两', '二')
    try: return int(cn2an.cn2an(s, 'smart'))
    except Exception: return 0

def natural_sort_key(name: str) -> tuple:
    """自然排序键：提取文件名里的章号。

    三级策略，缺一不可（每级修掉上一级引入的回归）：
    - 第 1 级 **开头即数字**：`1.txt` / `001.txt` / `第2章.txt`
      早期版本只做 `re.match`，结果 `正文-1.txt`、`Chapter 1.txt`、`楔子1.txt`
      全落到 0，多文件模式下正文顺序变成 1 → 10 → 2，整本书写反。
    - 第 2 级 **任意位置的 第N<单位>**：`我的书-第3章.txt`、`【第一章】初入江湖.txt`
      不做这一级的话，带前缀的文件会被排到最前面。
    - 第 3 级 **任意位置的数字**，但排除散文里的年份：`正文-1.txt`、`楔子1.txt`
      早期版本用无锚点的 re.search，把 `作者的话2024.txt` 当成第 2024 章、排到全书最后。

    都没有则回退 0，按原名排。
    """
    m = re.match(rf'^\s*(?:第\s*)?({_NUM})\s*[章回节卷部篇]?', name)
    if not m:
        m = re.search(rf'第\s*({_NUM})\s*[章回节卷部篇]', name)
    if m:
        return cn2num(m.group(1)), name
    # 第 3 级：裸数字，但要排除「的话2024」「完结于2023」这类年份
    m = re.search(_NUM, name)
    if m:
        v = cn2num(m.group(0))
        prev_cjk = m.start() > 0 and re.match(r'[一-鿿]', name[m.start() - 1])
        if not (prev_cjk and len(m.group(0)) == 4 and 1900 <= v <= 2099):
            return v, name
    return 0, name