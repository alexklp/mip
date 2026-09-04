-- 038_seed_segmentation_registry.sql
--
-- Registry entry for accepted content-segmentation v1 boundary classifier.
-- llm_model_id=1 already exists in llm_models and is not duplicated here.
--
-- Runtime worker substitutes the <<...>> placeholders for each adjacent
-- section pair before calling Mamay.

INSERT INTO segmentation_prompts
    (
        prompt_id,
        prompt_name,
        prompt_version,
        schema_version,
        prompt_text
    )
VALUES (
    1,
    'adjacent_boundary',
    'v1',
    'adjacent-boundary/1',
    $segmentation_prompt$Заголовок публікації: <<DOCUMENT_HEADING>>

Тобі надано ДВА сусідні структурні розділи (sections) цієї публікації, у порядку
появи в тексті. Виріши: правий розділ -- це продовження ТОГО САМОГО аналітичного
сюжету/наративу, що і лівий, чи це вже самостійний, окремий інфопривід.

СЕМАНТИКА:
- "same" -- обидва розділи є послідовними частинами ОДНОГО сюжету/наративу. Зміна
  героя, прикладу, аргументу, підаспекту чи підрозділу САМА ПО СОБІ не означає новий
  segment, якщо обидва розкривають один тезис публікації.
- "new" -- праворуч починається САМОСТІЙНИЙ інфопривід, який має сенс аналізувати
  НЕЗАЛЕЖНО від лівого розділу.

Орієнтуйся на заголовок публікації вище: якщо стаття має ЄДИНИЙ тезис, а розділи
розкривають його через різних людей/приклади -- це "same".

СУВОРІ ПРАВИЛА:
- Ти НЕ пояснюєш рішення, НЕ переписуєш текст, НЕ повертаєш offsets чи список
  segments.
- Відповідь -- СУВОРО один JSON-об'єкт з ОДНИМ полем "boundary", значення ЛИШЕ
  "same" або "new". Без жодного тексту до чи після, без markdown-огорожі, без
  коментарів.

### section_id=<<LEFT_SECTION_ID>> (лівий)
heading: <<LEFT_HEADING>>
text:
<<LEFT_TEXT>>

### section_id=<<RIGHT_SECTION_ID>> (правий)
heading: <<RIGHT_HEADING>>
text:
<<RIGHT_TEXT>>
$segmentation_prompt$
);
