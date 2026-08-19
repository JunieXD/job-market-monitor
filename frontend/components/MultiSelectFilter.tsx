"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, Search, X } from "lucide-react";

import type { SelectOption } from "@/components/SelectField";
import { matchesSubsequence } from "@/lib/search";

export function MultiSelectFilter({
  label,
  options,
  values,
  onValuesChange,
  ariaLabel,
  searchPlaceholder,
}: {
  label: string;
  options: SelectOption[];
  values: string[] | null;
  onValuesChange: (values: string[] | null) => void;
  ariaLabel: string;
  searchPlaceholder?: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const allValues = useMemo(() => options.map((option) => option.value), [options]);
  const effectiveValues = useMemo(
    () => new Set(values === null ? allValues : values),
    [allValues, values],
  );
  const filtered = useMemo(
    () => options.filter((option) => matchesSubsequence(option.label, query)),
    [options, query],
  );

  useEffect(() => {
    function closeOnOutsideClick(event: MouseEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    }
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", closeOnOutsideClick);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("mousedown", closeOnOutsideClick);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, []);

  const buttonLabel = useMemo(() => {
    if (values === null) return `全部${label}`;
    if (!values.length) return `${label} 0 项`;
    if (values.length === 1) {
      return options.find((option) => option.value === values[0])?.label ?? `${label} 1 项`;
    }
    return `${label} ${values.length} 项`;
  }, [label, options, values]);

  function commit(next: Set<string>) {
    const selected = allValues.filter((value) => next.has(value));
    onValuesChange(selected.length === allValues.length ? null : selected);
  }

  function toggle(value: string) {
    const next = new Set(effectiveValues);
    if (next.has(value)) next.delete(value);
    else next.add(value);
    commit(next);
  }

  function retainResults() {
    commit(new Set(filtered.map((option) => option.value)));
  }

  function selectResults() {
    const next = new Set(effectiveValues);
    filtered.forEach((option) => next.add(option.value));
    commit(next);
  }

  function deselectResults() {
    const next = new Set(effectiveValues);
    filtered.forEach((option) => next.delete(option.value));
    commit(next);
  }

  return (
    <div className="multi-select" ref={rootRef}>
      <button
        type="button"
        className={`multi-select-trigger ${values !== null ? "filtered" : ""}`}
        aria-label={ariaLabel}
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span>{buttonLabel}</span>
        <ChevronDown size={15} aria-hidden="true" />
      </button>
      {open && (
        <div className="multi-select-popover">
          <div className="filter-search">
            <Search size={15} aria-hidden="true" />
            <input
              autoFocus
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  retainResults();
                }
              }}
              placeholder={searchPlaceholder ?? `搜索${label}`}
              aria-label={`搜索${label}选项`}
            />
            {query && (
              <button type="button" className="clear-search" onClick={() => setQuery("")} aria-label="清除选项搜索" title="清除选项搜索">
                <X size={14} />
              </button>
            )}
          </div>
          <div className="filter-actions">
            <button type="button" onClick={retainResults} disabled={!filtered.length}>仅保留结果</button>
            <button type="button" onClick={selectResults} disabled={!filtered.length}>全选结果</button>
            <button type="button" onClick={deselectResults} disabled={!filtered.length}>取消结果</button>
            <button type="button" onClick={() => onValuesChange(null)}>清除筛选</button>
          </div>
          <div className="filter-options" role="listbox" aria-multiselectable="true" aria-label={`${label}选项`}>
            {filtered.length ? filtered.map((option) => {
              const selected = effectiveValues.has(option.value);
              return (
                <button
                  type="button"
                  role="option"
                  aria-selected={selected}
                  className="filter-option"
                  key={option.value}
                  onClick={() => toggle(option.value)}
                >
                  <span className={`filter-check ${selected ? "selected" : ""}`}>{selected && <Check size={13} />}</span>
                  <span>{option.label}</span>
                </button>
              );
            }) : <div className="filter-empty">没有匹配的选项</div>}
          </div>
        </div>
      )}
    </div>
  );
}
