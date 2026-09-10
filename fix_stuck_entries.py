#!/usr/bin/env python3
"""修复卡在 writing 状态的条目

当程序异常退出时，部分条目可能卡在 writing/read/write/verify 状态。
这个脚本会将它们重置为 ready 状态，以便继续迁移。
"""
import sqlite3
from pathlib import Path


def fix_stuck_entries(db_path="data/migration.db"):
    """修复卡在 writing 状态的条目"""
    db_file = Path(db_path)
    if not db_file.exists():
        print(f"错误：数据库文件不存在：{db_path}")
        return

    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()

    # 查询卡住的条目
    cursor.execute("SELECT COUNT(*) FROM entries WHERE status='writing'")
    stuck_count = cursor.fetchone()[0]

    if stuck_count == 0:
        print("没有发现卡住的条目，数据库状态正常。")
        conn.close()
        return

    print(f"发现 {stuck_count} 个卡在 writing 状态的条目")

    # 显示这些条目的详细信息
    cursor.execute(
        "SELECT subject_id, stage, attempts FROM entries WHERE status='writing'"
    )
    stuck_entries = cursor.fetchall()

    print("\n卡住的条目：")
    for sid, stage, attempts in stuck_entries:
        print(f"  - 条目 {sid}，阶段：{stage or '未知'}，尝试次数：{attempts}")

    # 询问是否修复
    response = input("\n是否将这些条目重置为 ready 状态？(y/n): ")
    if response.lower() != 'y':
        print("操作已取消")
        conn.close()
        return

    # 执行修复
    cursor.execute(
        "UPDATE entries SET status='ready', stage='', error='' WHERE status='writing'"
    )
    conn.commit()

    print(f"\n✓ 已成功修复 {stuck_count} 个条目")
    print("现在可以重启程序并继续自动迁移了。")

    # 显示当前状态统计
    cursor.execute(
        "SELECT status, COUNT(*) as count FROM entries GROUP BY status ORDER BY count DESC"
    )
    print("\n当前状态统计：")
    for status, count in cursor.fetchall():
        print(f"  {status}: {count}")

    conn.close()


if __name__ == "__main__":
    fix_stuck_entries()
