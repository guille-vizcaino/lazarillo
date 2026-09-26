{# One schema per target (analytics / dev) instead of dbt's default <target>_<custom> naming. #}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {{ target.schema }}
{%- endmacro %}
