"use client";

import * as Select from "@radix-ui/react-select";
import { Check, ChevronDown } from "lucide-react";

export type SelectOption = { value: string; label: string };

export function SelectField({
  label,
  value,
  options,
  onValueChange,
  ariaLabel,
  className = "",
  triggerClassName = "",
}: {
  label?: string;
  value: string;
  options: SelectOption[];
  onValueChange: (value: string) => void;
  ariaLabel: string;
  className?: string;
  triggerClassName?: string;
}) {
  return (
    <div className={`field ${className}`}>
      {label && <span className="field-label">{label}</span>}
      <Select.Root value={value} onValueChange={onValueChange}>
        <Select.Trigger className={`select-trigger ${triggerClassName}`} aria-label={ariaLabel}>
          <Select.Value />
          <Select.Icon className="select-icon"><ChevronDown size={15} /></Select.Icon>
        </Select.Trigger>
        <Select.Portal>
          <Select.Content className="select-content" position="popper" sideOffset={6}>
            <Select.Viewport className="select-viewport">
              {options.map((option) => (
                <Select.Item key={option.value} value={option.value} className="select-item">
                  <Select.ItemText>{option.label}</Select.ItemText>
                  <Select.ItemIndicator className="select-check"><Check size={14} /></Select.ItemIndicator>
                </Select.Item>
              ))}
            </Select.Viewport>
          </Select.Content>
        </Select.Portal>
      </Select.Root>
    </div>
  );
}
