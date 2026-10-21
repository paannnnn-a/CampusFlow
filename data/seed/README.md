# 校园事务种子数据

`get_affair_requirements` 的办理要求**不写在代码里**，而是从本文件读取。
每条记录必须带 `source` 字段说明出处，缺来源或缺 `required_fields` 的条目
会被直接丢弃（见 `backend/services/affairs.py` 的 `load_affair_specs`），
避免出现来路不明的"官方流程"。

## 文件结构

```json
{
  "affairs": [
    {
      "affair_type": "学生证补办",
      "aliases": ["学生证", "补办学生证", "学生证丢了"],
      "required_fields": ["name", "student_id", "reason"],
      "materials": ["身份证原件与复印件", "一寸证件照两张"],
      "steps": ["到学院办公室提交材料并填写申请表", "等待教务处制证"],
      "department": "学生事务中心",
      "source": "依据 XX 大学学生手册整理（本仓库为模拟整理）",
      "source_type": "simulated"
    }
  ]
}
```

## 字段说明

| 字段 | 必填 | 说明 |
|---|---|---|
| `affair_type` | 是 | 事务标准名称，会展示给用户 |
| `aliases` | 否 | 别名，用于匹配用户的自然说法 |
| `required_fields` | 是 | 生成草稿的必填字段名；缺一项即拒绝生成 |
| `materials` | 否 | 材料清单，直接展示 |
| `steps` | 否 | 办理步骤，按顺序展示 |
| `department` | 否 | 负责部门 |
| `source` | 是 | 出处。**缺此项的条目会被丢弃** |
| `source_type` | 否 | `simulated`（默认）或 `official` |

## 字段名约定

`required_fields` 用英文键名，中文标签在 `backend/services/affairs.py` 的
`FIELD_LABELS` 中维护。常用键：

| 键名 | 中文 |
|---|---|
| `name` | 姓名 |
| `student_id` | 学号 |
| `reason` | 申请说明 |
| `contact` | 联系电话 |
| `department` | 院系 |
| `class_name` | 班级 |
| `lost_date` | 遗失日期 |
| `lost_place` | 遗失地点 |
| `description` | 情况说明 |

## 与知识文档的关系

`campus_affairs.json` 提供**结构化**要求（材料清单、必填字段、步骤），
`data/knowledge/` 下的文档提供**可引用原文**。两者应互相对应：

- 事务的 `affair_type` 应与知识文档的 `source_id` 或标题一致；
- `materials` / `steps` 应与文档中的对应章节内容一致。

Agent 的调用顺序是「先检索知识 → 再查结构化要求 → 最后生成草稿」，
因此两者不一致时用户会先看到文档原文、再看到结构化摘要，矛盾会暴露出来。
写数据时应保持同步。

## 当前状态

**文件尚未创建。** 在提供事务资料前，`get_affair_requirements` 会返回
`UNSUPPORTED_AFFAIR` 并提示"暂未收录"，不会编造办理流程。
