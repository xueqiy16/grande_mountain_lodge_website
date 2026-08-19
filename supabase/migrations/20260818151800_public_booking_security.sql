-- ============================================================================
-- Public booking security hardening
-- ----------------------------------------------------------------------------
-- Supports the Flask backend changes that let it safely run with a server-side
-- Supabase Secret / service-role key. This migration:
--
--   1. Adds public.bookings.confirmation_token   -> high-entropy access token
--      that gates the public reservation-confirmation + calendar pages so guest
--      PII is not exposed by a guessable booking_reference alone.
--   2. Adds public.bookings.idempotency_key       -> per-attempt token so a
--      double-click / refresh / network retry cannot create duplicate bookings.
--   3. Adds a UNIQUE (idempotency_key, room_id) index -> DB-level duplicate
--      guard that survives concurrent requests.
--   4. Adds create_public_booking(...) -> a single transactional function that
--      creates the guest and ALL booking rows atomically, is idempotent on the
--      idempotency_key, INSERTS a fresh guest (never overwrites one by email),
--      and re-checks availability under a per-room lock before inserting.
--
-- It intentionally does NOT touch RLS/grants for bookings/guests. `anon` access
-- is revoked separately, later, after this code is deployed and verified.
--
-- Every statement is idempotent (IF NOT EXISTS / CREATE OR REPLACE). Run in the
-- Supabase SQL editor or via the Supabase CLI.
-- ============================================================================

-- --- 1 & 2. New columns -----------------------------------------------------
ALTER TABLE public.bookings
    ADD COLUMN IF NOT EXISTS confirmation_token text;

ALTER TABLE public.bookings
    ADD COLUMN IF NOT EXISTS idempotency_key text;

-- --- 3. Indexes -------------------------------------------------------------
-- Fast idempotency existence lookups.
CREATE INDEX IF NOT EXISTS idx_bookings_idempotency_key
    ON public.bookings (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- Concurrency-safe duplicate guard: one attempt cannot insert the same room
-- twice, and a replayed attempt collides instead of creating duplicates.
CREATE UNIQUE INDEX IF NOT EXISTS uq_bookings_idempotency_room
    ON public.bookings (idempotency_key, room_id)
    WHERE idempotency_key IS NOT NULL;

-- --- 4. Atomic public booking creation --------------------------------------
-- Parameters:
--   p_idempotency_key   text   per-attempt token from the client (or server)
--   p_booking_reference text   guest-facing reference (server generated)
--   p_confirmation_token text  high-entropy access token (server generated)
--   p_guest             jsonb  {first_name,last_name,email,phone,address,city,country}
--   p_bookings          jsonb  [ {room_id, check_in, check_out, adults, children,
--                                 pets, room_price, total_nights, total_price,
--                                 booking_notes} ]
-- Returns jsonb: { booking_reference, confirmation_token, reused }
--
-- SECURITY INVOKER (default): runs with the caller's privileges. Works with the
-- current anon key (which already has insert rights) and with the service-role
-- key after the switch. booking_status and amount_paid are forced server-side.
CREATE OR REPLACE FUNCTION public.create_public_booking(
    p_idempotency_key   text,
    p_booking_reference text,
    p_confirmation_token text,
    p_guest             jsonb,
    p_bookings          jsonb
) RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
    v_ref      text;
    v_token    text;
    v_guest_id public.guests.guest_id%TYPE;
    v_room_id  public.rooms.room_id%TYPE;
    elem       jsonb;
BEGIN
    IF p_idempotency_key IS NULL OR length(p_idempotency_key) < 16 THEN
        RAISE EXCEPTION 'invalid_idempotency_key';
    END IF;
    IF p_bookings IS NULL OR jsonb_array_length(p_bookings) = 0 THEN
        RAISE EXCEPTION 'no_bookings';
    END IF;

    -- Serialize concurrent submissions that share an idempotency key.
    PERFORM pg_advisory_xact_lock(hashtext('gml_book:' || p_idempotency_key));

    -- Idempotent replay: return the ORIGINAL reservation, do not create more.
    SELECT booking_reference, confirmation_token
      INTO v_ref, v_token
      FROM public.bookings
     WHERE idempotency_key = p_idempotency_key
     LIMIT 1;
    IF v_ref IS NOT NULL THEN
        RETURN jsonb_build_object(
            'booking_reference', v_ref,
            'confirmation_token', v_token,
            'reused', true
        );
    END IF;

    -- Create a NEW guest row (never overwrite an existing guest by email).
    BEGIN
        INSERT INTO public.guests (
            first_name, last_name, email, phone, address, city, country
        ) VALUES (
            p_guest->>'first_name',
            p_guest->>'last_name',
            p_guest->>'email',
            p_guest->>'phone',
            p_guest->>'address',
            p_guest->>'city',
            p_guest->>'country'
        )
        RETURNING guest_id INTO v_guest_id;
    EXCEPTION WHEN unique_violation THEN
        -- A UNIQUE constraint on guests.email blocks the safe insert-only path.
        -- Refuse rather than overwriting the existing guest's PII.
        RAISE EXCEPTION 'guest_email_conflict';
    END;

    -- Insert each booking row after re-checking availability under a per-room
    -- lock, inside this single transaction.
    FOR elem IN SELECT * FROM jsonb_array_elements(p_bookings)
    LOOP
        -- Resolve to the native-typed room_id (also validates the room exists).
        SELECT r.room_id INTO v_room_id
          FROM public.rooms r
         WHERE r.room_id::text = (elem->>'room_id');
        IF v_room_id IS NULL THEN
            RAISE EXCEPTION 'room_unavailable';
        END IF;

        -- Serialize concurrent bookings for the same physical room.
        PERFORM pg_advisory_xact_lock(hashtext('gml_room:' || (elem->>'room_id')));

        IF EXISTS (
            SELECT 1 FROM public.bookings b
             WHERE b.room_id = v_room_id
               AND b.booking_status NOT IN ('cancelled', 'no_show')
               AND b.check_in  < (elem->>'check_out')::date
               AND b.check_out > (elem->>'check_in')::date
        ) THEN
            RAISE EXCEPTION 'room_unavailable';
        END IF;

        INSERT INTO public.bookings (
            guest_id, room_id, check_in, check_out,
            adults, children, pets,
            booking_status, amount_paid,
            room_price, total_nights, total_price,
            booking_reference, booking_notes,
            idempotency_key, confirmation_token
        ) VALUES (
            v_guest_id, v_room_id,
            (elem->>'check_in')::date, (elem->>'check_out')::date,
            COALESCE((elem->>'adults')::int, 1),
            COALESCE((elem->>'children')::int, 0),
            COALESCE((elem->>'pets')::int, 0),
            'confirmed', 0,
            (elem->>'room_price')::numeric,
            (elem->>'total_nights')::int,
            (elem->>'total_price')::numeric,
            p_booking_reference,
            NULLIF(elem->>'booking_notes', ''),
            p_idempotency_key, p_confirmation_token
        );
    END LOOP;

    RETURN jsonb_build_object(
        'booking_reference', p_booking_reference,
        'confirmation_token', p_confirmation_token,
        'reused', false
    );
END;
$$;

-- The backend calls this via PostgREST RPC. Allow the roles the backend may run
-- as (anon during transition, service_role after the switch).
GRANT EXECUTE ON FUNCTION public.create_public_booking(text, text, text, jsonb, jsonb)
    TO anon, authenticated, service_role;
