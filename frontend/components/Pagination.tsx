"use client";

import { useEffect, useMemo, useState } from "react";
import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from "lucide-react";

import { SelectField } from "@/components/SelectField";

const defaultPageSizes = [5, 10, 20, 50, 100];

export function Pagination({
  total,
  page,
  pageSize,
  onPageChange,
  onPageSizeChange,
  itemLabel = "条记录",
  pageSizes = defaultPageSizes,
  maxPageSize = 100,
}: {
  total: number;
  page: number;
  pageSize: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: number) => void;
  itemLabel?: string;
  pageSizes?: number[];
  maxPageSize?: number;
}) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const currentPage = Math.min(Math.max(1, page), pageCount);
  const [pageDraft, setPageDraft] = useState(String(currentPage));
  const [customDraft, setCustomDraft] = useState(String(pageSize));
  const [customMode, setCustomMode] = useState(!pageSizes.includes(pageSize));
  const sizeOptions = useMemo(
    () => [...pageSizes.map((size) => ({ value: String(size), label: `${size} 条/页` })), { value: "custom", label: "自定义" }],
    [pageSizes],
  );

  useEffect(() => { setPageDraft(String(currentPage)); }, [currentPage]);
  useEffect(() => {
    setCustomDraft(String(pageSize));
    setCustomMode(!pageSizes.includes(pageSize));
  }, [pageSize, pageSizes]);

  const firstItem = total ? (currentPage - 1) * pageSize + 1 : 0;
  const lastItem = Math.min(total, currentPage * pageSize);

  function applyPageDraft() {
    const next = Number.parseInt(pageDraft, 10);
    if (Number.isFinite(next)) onPageChange(Math.min(Math.max(1, next), pageCount));
    else setPageDraft(String(currentPage));
  }

  function applyCustomSize() {
    const next = Number.parseInt(customDraft, 10);
    if (Number.isFinite(next)) {
      onPageSizeChange(Math.min(Math.max(1, next), maxPageSize));
      setCustomMode(true);
    } else {
      setCustomDraft(String(pageSize));
    }
  }

  return (
    <nav className="pagination" aria-label="分页导航">
      <div className="pagination-summary">
        <strong>{firstItem}-{lastItem}</strong>
        <span>/ 共 {total.toLocaleString("zh-CN")} {itemLabel}</span>
      </div>
      <div className="pagination-size">
        <SelectField
          value={customMode ? "custom" : String(pageSize)}
          options={sizeOptions}
          onValueChange={(value) => {
            if (value === "custom") setCustomMode(true);
            else {
              setCustomMode(false);
              onPageSizeChange(Number(value));
            }
          }}
          ariaLabel="选择每页数量"
          triggerClassName="pagination-size-trigger"
        />
        {customMode && (
          <input
            className="pagination-custom-input"
            inputMode="numeric"
            value={customDraft}
            onChange={(event) => setCustomDraft(event.target.value.replace(/\D/g, ""))}
            onBlur={applyCustomSize}
            onKeyDown={(event) => { if (event.key === "Enter") applyCustomSize(); }}
            aria-label="自定义每页数量"
          />
        )}
      </div>
      <div className="pagination-pages">
        <button type="button" className="icon-button" onClick={() => onPageChange(1)} disabled={currentPage === 1} aria-label="首页" title="首页"><ChevronsLeft size={17} /></button>
        <button type="button" className="icon-button" onClick={() => onPageChange(currentPage - 1)} disabled={currentPage === 1} aria-label="上一页" title="上一页"><ChevronLeft size={17} /></button>
        <span className="page-jump">第 <input inputMode="numeric" value={pageDraft} onChange={(event) => setPageDraft(event.target.value.replace(/\D/g, ""))} onBlur={applyPageDraft} onKeyDown={(event) => { if (event.key === "Enter") applyPageDraft(); }} aria-label="跳转到页码" /> / {pageCount} 页</span>
        <button type="button" className="icon-button" onClick={() => onPageChange(currentPage + 1)} disabled={currentPage >= pageCount} aria-label="下一页" title="下一页"><ChevronRight size={17} /></button>
        <button type="button" className="icon-button" onClick={() => onPageChange(pageCount)} disabled={currentPage >= pageCount} aria-label="尾页" title="尾页"><ChevronsRight size={17} /></button>
      </div>
    </nav>
  );
}
