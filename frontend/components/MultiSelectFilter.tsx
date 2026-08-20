"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, Search, X } from "lucide-react";

import { CompanyLogo } from "@/components/CompanyLogo";
import type { SelectOption } from "@/components/SelectField";
import { matchesSubsequence } from "@/lib/search";

export function MultiSelectFilter({
  label,
  options,
  values,
  onValuesChange,
  ariaLabel,
  searchPlaceholder,
  allSelectedLabel,
  className = "",
}: {
  label: string;
  options: SelectOption[];
  values: string[] | null;
  onValuesChange: (values: string[] | null) => void;
  ariaLabel: string;
  searchPlaceholder?: string;
  allSelectedLabel?: string;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const autoRetainResultsRef = useRef(false);
  const allValues = useMemo(() => options.map((option) => option.value), [options]);
  const effectiveValues = useMemo(
    () => new Set(values === null ? allValues : values),
    [allValues, values],
  );
  const filtered = useMemo(
    () => options.filter((option) => matchesSubsequence(option.label, query)),
    [options, query],
  );
  const selectedResultCount = useMemo(
    () => filtered.filter((option) => effectiveValues.has(option.value)).length,
    [effectiveValues, filtered],
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
    if (values === null) return allSelectedLabel ?? `全部${label}`;
    if (!values.length) return `${label} 0 项`;
    if (values.length === 1) {
      return options.find((option) => option.value === values[0])?.label ?? `${label} 1 项`;
    }
    return `${label} ${values.length} 项`;
  }, [allSelectedLabel, label, options, values]);

  function commit(next: Set<string>, manual = true) {
    if (manual) autoRetainResultsRef.current = false;
    const selected = allValues.filter((value) => next.has(value));
    onValuesChange(selected.length === allValues.length ? null : selected);
  }

  function updateQuery(nextQuery: string) {
    if (!query && nextQuery && values === null) {
      autoRetainResultsRef.current = true;
    } else if (!nextQuery) {
      autoRetainResultsRef.current = false;
    }
    setQuery(nextQuery);
    if (autoRetainResultsRef.current) {
      const matchingValues = options
        .filter((option) => matchesSubsequence(option.label, nextQuery))
        .map((option) => option.value);
      commit(new Set(matchingValues), false);
    }
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

  function selectAll() {
    commit(new Set(allValues));
  }

  function deselectAll() {
    commit(new Set());
  }

  return (
    <div className={`multi-select ${className}`} ref={rootRef}>
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
              value={query}
              onChange={(event) => updateQuery(event.target.value)}
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
              <button type="button" className="clear-search" onClick={() => updateQuery("")} aria-label="清除选项搜索" title="清除选项搜索">
                <X size={14} />
              </button>
            )}
          </div>
          <div className="filter-actions">
            {query && <button type="button" onClick={retainResults} disabled={!filtered.length || (effectiveValues.size === filtered.length && selectedResultCount === filtered.length)}>仅选搜索结果</button>}
            {query && <button type="button" onClick={selectResults} disabled={!filtered.length || selectedResultCount === filtered.length}>选中搜索结果</button>}
            {query && <button type="button" onClick={deselectResults} disabled={!selectedResultCount}>取消搜索结果</button>}
            <button type="button" onClick={selectAll} disabled={!allValues.length || effectiveValues.size === allValues.length}>全部选中</button>
            <button type="button" onClick={deselectAll} disabled={!effectiveValues.size}>全部取消</button>
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
                  <span className="filter-option-label">
                    {option.companyKey && <CompanyLogo companyKey={option.companyKey} companyName={option.label} />}
                    <span>{option.label}</span>
                  </span>
                </button>
              );
            }) : <div className="filter-empty">没有匹配的选项</div>}
          </div>
        </div>
      )}
    </div>
  );
}
