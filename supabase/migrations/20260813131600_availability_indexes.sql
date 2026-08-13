-- ============================================================================
-- Availability / booking-overlap performance indexes
-- ----------------------------------------------------------------------------
-- These indexes back the server-side availability logic in main.py:
--
--   * _overlap_counts_by_code()  -> per room-type overlap scan on public.bookings
--   * _available_room_ids()      -> per physical room overlap scan on public.bookings
--   * _inventory_by_code()       -> operational physical base on public.rooms
--
-- Query shape (half-open interval, active bookings only):
--     WHERE check_in  <  :requested_check_out
--       AND check_out >  :requested_check_in
--       AND booking_status NOT IN ('cancelled', 'no_show')
--
-- The booking indexes are PARTIAL: they only index rows that can actually block
-- inventory (active bookings), which keeps them small and skips cancelled /
-- no-show rows entirely. Run this whole file in the Supabase SQL editor (or via
-- the Supabase CLI). Every statement is idempotent (IF NOT EXISTS).
-- ============================================================================

-- --- public.bookings --------------------------------------------------------

-- Room-type availability scans: range predicates on the two date columns.
-- Leading column check_out supports `check_out > :req_in` (which also excludes
-- past bookings); check_in narrows the upper bound `check_in < :req_out`.
CREATE INDEX IF NOT EXISTS idx_bookings_active_dates
    ON public.bookings (check_out, check_in)
    WHERE booking_status NOT IN ('cancelled', 'no_show');

-- Concrete room-assignment guard: overlap check scoped to a set of room_ids.
-- room_id first (equality/IN), then the date range columns.
CREATE INDEX IF NOT EXISTS idx_bookings_room_active_dates
    ON public.bookings (room_id, check_out, check_in)
    WHERE booking_status NOT IN ('cancelled', 'no_show');

-- --- public.rooms -----------------------------------------------------------

-- Operational physical base per room-type code, filtered by status
-- (status NOT IN ('house-keeping','out-of-service')).
CREATE INDEX IF NOT EXISTS idx_rooms_code_status
    ON public.rooms (code, status);

-- Same lookup keyed by the room_type_id foreign key (used by joins / room_type
-- oriented queries).
CREATE INDEX IF NOT EXISTS idx_rooms_type_status
    ON public.rooms (room_type_id, status);

-- Refresh planner statistics so the new indexes are considered immediately.
ANALYZE public.bookings;
ANALYZE public.rooms;
