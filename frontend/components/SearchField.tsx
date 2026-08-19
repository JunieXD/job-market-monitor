"use client";

import { Search } from "lucide-react";

import { MultiSelectFilter } from "@/components/MultiSelectFilter";
import type { SelectOption } from "@/components/SelectField";

export function SearchField({
  value,
  onValueChange,
  scopesValue,
  scopes,
  onScopesChange,
  placeholder,
  ariaLabel,
}: {
  value: string;
  onValueChange: (value: string) => void;
  scopesValue: string[] | null;
  scopes: SelectOption[];
  onScopesChange: (value: string[] | null) => void;
  placeholder: string;
  ariaLabel: string;
}) {
  return (
    <div className="search-control">
      <Search size={16} aria-hidden="true" />
      <input
        value={value}
        onChange={(event) => onValueChange(event.target.value)}
        placeholder={placeholder}
        aria-label={ariaLabel}
      />
      <MultiSelectFilter
        label="搜索字段"
        options={scopes}
        values={scopesValue}
        onValuesChange={onScopesChange}
        ariaLabel="选择搜索字段"
        allSelectedLabel={`${scopes.length} 个字段`}
        minimumSelected={1}
        className="search-scope-field"
      />
    </div>
  );
}
