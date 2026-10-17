# Data lineage questions

When the user asks where a column/table's data comes from, what feeds it, or what is impacted by a
change, use the `codelineage` MCP tools (`find_columns`, `trace_column_lineage`,
`trace_table_lineage`, `describe_table`, `read_sql_file`). Do not answer lineage questions by
guessing from file names; the tools are built by parsing every SQL file, Airflow DAG and config file.

Answer with: a short summary, a "Lineage by level" table (Level | Column | Transformation | Built in
file / DAG.task), filters & joins that affect the values, the ultimate SOURCE tables, and the mermaid
diagram from the tool output.
