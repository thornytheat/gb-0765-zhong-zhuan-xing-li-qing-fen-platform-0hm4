"""处置单的纯规则：建单元数据、撤下回放确认、待处置件数。"""


def wrong_bags(stages):
    """从错载与漏扫阶段取错装记录，按 (牌, 错装航段) 去重。"""
    seen, bags = set(), []
    for row in stages['exceptions']['wrong']:
        key = row['tag'], row['leg']
        if key not in seen:
            seen.add(key)
            bags.append(dict(tag=row['tag'], leg=row['leg'], planned=row.get('planned')))
    return sorted(bags, key=lambda item: item['tag'])


def offload_confirmed(stages, tag, leg):
    """回放末态为撤下，且时间线存在 OFFLOAD 采信记录，才允许关单。"""
    state = next((item for item in stages['replay']['states']
                  if item['tag'] == tag and item['leg'] == leg), None)
    if state is None or state['state'] != 'OFFLOADED':
        return False
    return any(row['tag'] == tag and row['leg'] == leg and row['action'] == 'OFFLOAD'
               for row in stages['replay']['rows'])


def pending_tally(orders):
    """对账联动：待处置为未关单（按牌去重），已处置为已关单。"""
    open_tags, closed_tags = set(), set()
    for order in orders:
        (closed_tags if order['status'] == 'closed' else open_tags).add(order['tag'])
    return dict(disposition_open=len(open_tags), disposition_closed=len(closed_tags),
                disposition_total=len(open_tags | closed_tags))
