
from __future__ import annotations
import json
import pandas as pd
from dataclasses import dataclass
from typing import List, Dict, Optional, Sequence

# Беремо task/rules для image/video з конфіга
from Script.config import ADMIN_PROMPT_SCHEMA


@dataclass
class PromptParts:
    df_columns: List[str]   # колонки таблиці без 'FileName'
    df_header: List[str]    # df_columns + ['FileName']
    prompt_text: str        # згенерований адмін-промпт


class PromptBuilder:
    """
    Працює зі схемою адмін-промптів (JSON):
      {
        "columns": ["№", "Description", "Brand", "Product", "Category"],
        "image": {
          "column_rules": { "<col>": "...", ... },
          "column_examples": { "<col>": "...", ... }
        },
        "video": {
          "column_rules": { "<col>": "...", ... },
          "column_examples": { "<col>": "...", ... }
        }
      }

    - Якщо файл відсутній і create_if_missing=True - створюється порожня валідна схема.
    - Додавання колонки вимагає ПРАВИЛ та ПРИКЛАДІВ для обох режимів (image & video).
    - TASK/RULES беруться з config.ADMIN_PROMPT_SCHEMA (єдина константа).
    """

    PROTECTED_COLUMNS = {"№", "FileName"}
    CLASSIFICATION_COL = "Classification"

    def __init__(self, schema_path: str, create_if_missing: bool = True) -> None:
        self.schema_path = schema_path
        self._classification_items: list[str] | None = None
        self._classification_enabled: bool = False

        try:
            with open(schema_path, "r", encoding="utf-8") as f:
                self.schema = json.load(f)
            self._ensure_minimal_structure()
        except FileNotFoundError:
            if not create_if_missing:
                raise

            self.schema = {
                "columns": ["№"],
                "image": {"column_rules": {"№": "Image number"}, "column_examples": {"№": "1"}},
                "video": {"column_rules": {"№": "Video number"}, "column_examples": {"№": "1"}},
            }

        if "№" in self.schema["columns"]:
            self.schema["columns"].remove("№")
            self.schema["columns"].insert(0, "№")

        self._validate(strict=False)

    # --------- ПУБЛІЧНЕ API ---------
    @property
    def columns(self) -> List[str]:
        return list(self.schema.get("columns", []))

    @property
    def df_header(self) -> List[str]:
        """
        :return: Узгоджений хедер для DF: колонки таблиці + FileName
        """
        return self.columns + ["FileName"]

    @property
    def table_header_md(self) -> str:
        """
        :return: Markdown-заголовок таблиці у потрібному порядку
        """
        return " | ".join(self.columns)

    def add_column(
        self,
        name: str,
        *,
        image_rule: str,
        image_example: str,
        video_rule: str,
        video_example: str,
        position: Optional[int] = None,
        overwrite: bool = False,
    ) -> None:
        name = str(name).strip()
        if not name:
            raise ValueError("Column name must be a non-empty string")

        if name in self.PROTECTED_COLUMNS:
            raise ValueError(f"Column '{name}' is protected and cannot be added or modified manually")

        if name == self.CLASSIFICATION_COL:
            raise ValueError("Column 'Classification' is special. Use add_classification_sheet(...) to add it.")

        cols = self.schema["columns"]
        exists = name in cols

        if exists and not overwrite:
            raise ValueError(f"Column '{name}' already exists. Use overwrite=True to modify.")


        if exists:
            if position is not None:
                cols.remove(name)
                pos = max(1, min(position, len(cols)))
                cols.insert(pos, name)
        else:
            pos = len(cols) if position is None else max(1, min(position, len(cols)))
            cols.insert(pos, name)


        for mode, rule_text, example_text in (("image", image_rule, image_example), ("video", video_rule, video_example)):
            self.schema[mode]["column_rules"][name] = str(rule_text).strip()
            self.schema[mode]["column_examples"][name] = str(example_text).strip()

        self._validate(strict=True)

    def remove_column(self, name: str) -> None:
        name = str(name).strip()
        if name in self.PROTECTED_COLUMNS:
            raise ValueError(f"Column '{name}' is protected and cannot be removed")

        if name not in self.schema["columns"]:
            raise ValueError(f"Column '{name}' not found")

        self.schema["columns"].remove(name)
        for mode in ("image", "video"):
            self.schema[mode]["column_rules"].pop(name, None)
            self.schema[mode]["column_examples"].pop(name, None)

        self._validate(strict=True)

    def add_classification_sheet(
            self,
            *,
            items: list[str] | None = None,
            excel_path: str | None = None,
            column: str | None = None,
            column_index: int | None = None,
            max_items: int | None = 200,
            position: int | None = 1,
            image_rule: str,
            image_example: str | None = None,
            video_rule: str,
            video_example: str | None = None,
            overwrite: bool = False,
    ) -> None:
        """
        Додає/вмикає спеціальну колонку 'Classification' і підшиває перелік значень.
        ЦЮ колонку не можна додавати через add_column().
        """
        name = self.CLASSIFICATION_COL
        cols = self.schema["columns"]
        exists = name in cols

        if name in self.PROTECTED_COLUMNS:
            raise ValueError(f"Column '{name}' is protected and cannot be modified")

        if exists and not overwrite:
            pass
        else:
            if exists:
                cols.remove(name)
            pos = 1 if position is None else max(1, min(position, len(cols)))
            cols.insert(pos, name)

        # Перелік (лист) класифікації
        items_list = items
        if items_list is None and excel_path:
            items_list = self._load_catalog_from_excel(excel_path, column=column, column_index=column_index, max_items=max_items)

        if not items_list:
            raise LookupError("Items list for file classification not found")

        if not image_example:
            image_example = items_list[0]
        if not video_example:
            video_example = items_list[0]

        # Правила/приклади обов'язково
        for mode, rule_text, example_text in (("image", image_rule, image_example), ("video", video_rule, video_example)):
            self.schema[mode]["column_rules"][name] = str(rule_text).strip()
            self.schema[mode]["column_examples"][name] = str(example_text).strip()

        self._classification_items = [s for s in (items_list or []) if str(s).strip()]
        self._classification_enabled = True

        self._validate(strict=True)

    def save(self, path: Optional[str] = None) -> None:
        out = path or self.schema_path
        with open(out, "w", encoding="utf-8") as f:
            json.dump(self.schema, f, ensure_ascii=False, indent=2) # noqa

    def build_image_prompt(self) -> PromptParts:
        base = self._compose_prompt("image")
        if self._classification_enabled and self._classification_items:
            base = self._append_catalog_block(base, self._classification_items, title="CLASSIFICATION LIST")

        return PromptParts(df_columns=self.columns, df_header=self.df_header, prompt_text=base)

    def build_video_prompt(self) -> PromptParts:
        base = self._compose_prompt("video")
        if self._classification_enabled and self._classification_items:
            base = self._append_catalog_block(base, self._classification_items, title="CLASSIFICATION LIST")

        return PromptParts(df_columns=self.columns, df_header=self.df_header, prompt_text=base)

    # --------- ВНУТРІШНЄ ---------
    def _ensure_minimal_structure(self) -> None:
        """
        Гарантує мінімально необхідні ключі/типи в schema.
        """
        s = self.schema
        s.setdefault("columns", [])
        s.setdefault("image", {})
        s.setdefault("video", {})
        s["image"].setdefault("column_rules", {})
        s["image"].setdefault("column_examples", {})
        s["video"].setdefault("column_rules", {})
        s["video"].setdefault("column_examples", {})

        # виправляємо типи якщо хтось випадково поклав не ті
        if not isinstance(s["columns"], list):
            s["columns"] = list(s["columns"]) if s["columns"] is not None else []
        for mode in ("image", "video"):
            for key in ("column_rules", "column_examples"):
                if not isinstance(s[mode][key], dict):
                    s[mode][key] = dict(s[mode][key]) if s[mode][key] is not None else {}

    def _validate(self, strict: bool = True) -> None:
        """
        Перевіряє, що:
          - columns -> список непорожніх унікальних назв;
          - для КОЖНОЇ колонки є правила та приклади в image & video.
        Якщо strict=False, пом'якшується перевірка (без жорстких вимог на повноту).
        """
        cols = self.schema.get("columns", [])
        if not isinstance(cols, list):
            raise ValueError("'columns' must be a list")
        if any(not str(c).strip() for c in cols):
            raise ValueError("All column names must be non-empty strings")
        if len(set(cols)) != len(cols):
            raise ValueError("Column names must be unique")

        for mode in ("image", "video"):
            section = self.schema.get(mode, {})
            rules = section.get("column_rules", {})
            examples = section.get("column_examples", {})
            if not isinstance(rules, dict) or not isinstance(examples, dict):
                raise ValueError(f"'{mode}.column_rules' and '{mode}.column_examples' must be dicts")

            if strict:
                missing_rules = [c for c in cols if c not in rules]
                missing_examples = [c for c in cols if c not in examples]
                if missing_rules:
                    raise ValueError(f"Missing {mode}.column_rules for: {missing_rules}")
                if missing_examples:
                    raise ValueError(f"Missing {mode}.column_examples for: {missing_examples}")

    @staticmethod
    def _load_catalog_from_excel(excel_path: str, column: str | None = None, column_index: int | None = None, max_items: int | None = 200) -> list[str]:
        df = pd.read_excel(excel_path)
        if column is not None and column in df.columns:
            ser = df[column]
        elif column_index is not None and 0 <= column_index < df.shape[1]:
            ser = df.iloc[:, column_index]
        else:
            text_cols = df.select_dtypes(include=["object"])
            if text_cols.shape[1] == 0:
                raise ValueError("Не знайдено текстових колонок у Excel для каталогу")
            ser = text_cols.iloc[:, 0]

        ser = ser.ffill().astype(str).str.strip()
        items = [x for x in ser.tolist() if x]
        if max_items is not None:
            items = items[:max_items]
        return items

    @staticmethod
    def _append_catalog_block(prompt_text: str, catalog_items: Sequence[str], title: str = "CLASSIFICATION LIST") -> str:
        if not catalog_items:
            return prompt_text
        lines = [
            "",
            "(Hint: List for a 'Classification' column, write words, NOT a number)",
            f"{title}:",
        ]
        lines.extend([f"{i + 1}. {item}" for i, item in enumerate(catalog_items)])
        return prompt_text + "\n" + "\n".join(lines)

    def _compose_prompt(self, mode: str) -> str:
        """
        Формує текст адмін-промпта для 'image' або 'video'.

        Структура:
          <TASK з config.ADMIN_PROMPT_SCHEMA>
          RULES:
            1. <rule1>
            2. <rule2>
          OUTPUT as Markdown table with columns:
          <col1 | col2 | ...>

          TABLE columns describe:
          "col1" - <rule>
          ...
          Example row:
          <example values joined by ' | '>
        """
        if mode not in ("image", "video"):
            raise ValueError("mode must be 'image' or 'video'")

        spec = ADMIN_PROMPT_SCHEMA.get(mode)
        if not spec or "task" not in spec or "rules" not in spec:
            raise ValueError(f"config.ADMIN_PROMPT_SCHEMA['{mode}'] must contain 'task' and 'rules'")

        cols = self.columns
        if not cols:
            raise ValueError("No columns configured. Add at least one column before building prompts.")

        # описи колонок і приклад рядка - з JSON
        section = self.schema[mode]
        rules_map: Dict[str, str] = section["column_rules"]
        ex_map: Dict[str, str] = section["column_examples"]

        # опис кожної колонки
        lines: List[str] = [
            f"OUTPUT as Markdown table with columns:\n{self.table_header_md}\n",
            "TABLE columns describe:",
        ]
        for col in cols:
            lines.append(f"\"{col}\" - {rules_map[col].strip()}")

        # приклад рядка
        example_row = " | ".join(ex_map[c].strip() for c in cols)
        lines.append("\nExample row:")
        lines.append(example_row)

        # збірка
        prompt_lines: List[str] = [spec["task"].strip(), "RULES:"]
        prompt_lines.extend(f"{i+1}. {str(r).strip()}" for i, r in enumerate(spec["rules"]))
        prompt_lines.append("")
        prompt_lines.extend(lines)

        return "\n".join(prompt_lines)


# --------- Приклад використання ---------
if __name__ == "__main__":
    from Script.config import APP_CONFIG

    admin_prompt_schema = APP_CONFIG['paths']['admin_prompt_schema']
    classification_excel = APP_CONFIG['paths']['classification_excel']

    pb = PromptBuilder(admin_prompt_schema, create_if_missing=True)

    if len(pb.columns) == 1:
        pb.add_column(
            "Brand",
            image_rule="company or trademark (e.g. Samsung, L'Oreal), or 'Not found'", image_example="Samsung",
            video_rule="company or trademark (e.g. Samsung, L'Oreal), or 'Not found'", video_example="Samsung",
            overwrite=True
        )
        pb.add_column(
            "Product",
            image_rule="model name/number (e.g. S24, A15), or 'Not found'", image_example="S24",
            video_rule="model name/number (e.g. S24, A15), or 'Not found'", video_example="S24",
            overwrite=True
        )
        pb.add_column(
            "Category",
            image_rule="general type (e.g. kitchen grill, gaming laptop, throat lozenges), or 'Not found'", image_example="Smartphone",
            video_rule="general type (e.g. kitchen grill, gaming laptop, throat lozenges), or 'Not found'", video_example="Smartphone",
        )
        pb.add_column(
            "Description",
            image_rule="A very brief description of what is in the image", image_example="Front view of Galaxy S24 display",
            video_rule="A very brief description of what is in the video", video_example="Front view of Galaxy S24 display",
            overwrite=True
        )

        # pb.save()  # збережемо базову схему

    # pb.add_classification_sheet(
    #     excel_path=classification_excel,
    #     column_index=2,  # 3-я колонка в Excel
    #     max_items=300,
    #     image_rule="Pick the best matching label from the classification list; else 'Not classified'.",
    #     video_rule="Pick the best matching label from the classification list using all frames; else 'Not classified'.",
    #     position=1,  # після '№'
    #     overwrite=True
    # )

    image_prompt = pb.build_image_prompt().prompt_text
    video_prompt = pb.build_video_prompt().prompt_text

    print("DF Header:", pb.df_header)
    print("\n\n--- IMAGE ADMIN PROMPT ---\n")
    print(image_prompt)
    print("\n\n--- VIDEO ADMIN PROMPT ---\n")
    print(video_prompt)
