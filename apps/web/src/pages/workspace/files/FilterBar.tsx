/**
 * The filter bar beside the search field.
 *
 * It renders the chip registry in `filterState.ts` and nothing else: a new facet
 * is a row in that registry, not a new control here. Chips are toggle buttons
 * with `aria-pressed`, grouped so a screen reader announces "Kind, Folders,
 * pressed" rather than a wall of unlabelled buttons.
 *
 * The bar never talks to the server. It writes the filter state, the state
 * writes the URL, and the listing and search hooks read the state — which is why
 * a filtered view is linkable and why a chip means exactly the same thing in a
 * folder listing as it does in a search.
 */

import { useState } from "react";

import {
  CHIP_GROUPS,
  activeFilterLabels,
  chipsOfGroup,
  clearFilters,
  hasActiveFilters,
  toggleChip,
  type FilterState,
} from "./filterState";

export interface FilterBarProps {
  state: FilterState;
  onChange: (next: FilterState) => void;
}

/**
 * How many chips sit past the first group — the ones a one-row bar makes you scroll to.
 *
 * The bar is a single scrolling row (wrapping cost the head three rows at 1440 and
 * overflowed the viewport at 390), so the registry beyond the leading group is overflow.
 * The summary button counts it and, pressed, lets the bar wrap so all of it is on screen
 * at once. Nothing is ever removed from the DOM: an overflowed chip is one scroll away,
 * not gone, so the keyboard and a screen reader still reach every filter.
 *
 * Exported because the count is the contract, and a test should be able to state it
 * without measuring a layout jsdom does not have.
 */
export function overflowChipCount(): number {
  return CHIP_GROUPS.slice(1).reduce((total, group) => total + chipsOfGroup(group.id).length, 0);
}

export function FilterBar({ state, onChange }: FilterBarProps) {
  const active = hasActiveFilters(state);
  const [expanded, setExpanded] = useState(false);
  const overflow = overflowChipCount();
  return (
    <div
      className="alk-files-filters"
      role="group"
      aria-label="Filters"
      data-expanded={expanded ? "true" : undefined}
    >
      {CHIP_GROUPS.map((group) => (
        <div
          key={group.id}
          className="alk-files-filters__group"
          role="group"
          aria-label={group.label}
        >
          <span className="alk-files-filters__group-label">{group.label}</span>
          {chipsOfGroup(group.id).map((chip) => {
            const on = state.chips.includes(chip.id);
            return (
              <button
                key={chip.id}
                type="button"
                className="alk-files-filters__chip"
                data-chip={chip.id}
                aria-pressed={on}
                onClick={() => onChange(toggleChip(state, chip.id))}
              >
                {chip.label}
              </button>
            );
          })}
        </div>
      ))}
      {overflow > 0 ? (
        <button
          type="button"
          className="alk-files-filters__more"
          aria-expanded={expanded}
          onClick={() => setExpanded((was) => !was)}
        >
          {expanded ? "Fewer filters" : `+${overflow} more filters`}
        </button>
      ) : null}
      {active ? (
        <button
          type="button"
          className="alk-files-filters__clear"
          onClick={() => onChange(clearFilters(state))}
        >
          Clear filters
        </button>
      ) : null}
    </div>
  );
}

export interface FilteredEmptyStateProps {
  state: FilterState;
  onChange: (next: FilterState) => void;
}

/**
 * What the browser shows when the filters have emptied it.
 *
 * It names every active filter, because "no items" with a chip quietly on three
 * screens back is the single most confusing state a file manager can be in, and
 * it offers the one action that fixes it.
 */
export function FilteredEmptyState({ state, onChange }: FilteredEmptyStateProps) {
  const labels = activeFilterLabels(state);
  if (labels.length === 0) {
    return (
      <div className="alk-files-filters__empty" role="status">
        This folder is empty.
      </div>
    );
  }
  return (
    <div className="alk-files-filters__empty" role="status">
      <p className="alk-files-filters__empty-text">Nothing here matches {labels.join(" and ")}.</p>
      <button
        type="button"
        className="alk-files-filters__clear"
        onClick={() => onChange(clearFilters(state))}
      >
        Clear filters
      </button>
    </div>
  );
}
