"use client";

import { Search } from "lucide-react";

import { SelectField, type SelectOption } from "@/components/SelectField";

export function SearchField({
  value,
  onValueChange,
  scope,
  scopes,
  onScopeChange,
  placeholder,
  ariaLabel,
}: {
  value: string;
  onValueChange: (value: string) => void;
  scope: string;
  scopes: SelectOption[];
  onScopeChange: (value: string) => void;
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
      <SelectField
        value={scope}
        options={scopes}
        onValueChange={onScopeChange}
        ariaLabel="选择搜索字段"
        className="search-scope-field"
        triggerClassName="search-scope-trigger"
      />
    </div>
  );
}
