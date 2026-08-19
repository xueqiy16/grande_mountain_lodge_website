-- ============================================================================
-- Public guest self-service cancellation
-- ----------------------------------------------------------------------------
-- Adapts existing public.cancellation for hashed, one-time, expiring tokens
-- and adds cancel_public_booking(...) so Flask (service_role only) can cancel
-- an entire reservation atomically.
--
-- Authorization is the SHA-256 hash of a server-generated token
-- (secrets.token_urlsafe(32)). The leftover uuid column `cancellation_token`
-- is NOT used for authorization. It remains NOT NULL for compatibility with
-- the existing schema; new rows store a dummy gen_random_uuid(). It can be
-- dropped later once no legacy UUID-token rows are needed.
--
-- One cancellation row is created per reservation (booking_reference),
-- pointing at one of that reservation's booking_id values. Cancelling
-- updates EVERY public.bookings row that shares the booking_reference.
--
-- RLS: no anon/authenticated table access. service_role bypasses RLS.
-- cancel_public_booking EXECUTE is granted only to service_role.
-- ============================================================================

-- --- 1. Columns -------------------------------------------------------------
ALTER TABLE public.cancellation
    ADD COLUMN IF NOT EXISTS cancellation_token_hash text;

ALTER TABLE public.cancellation
    ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();

ALTER TABLE public.cancellation
    ALTER COLUMN id SET DEFAULT gen_random_uuid();

ALTER TABLE public.cancellation
    ALTER COLUMN token_usage SET DEFAULT false;

-- LodgeOS already writes this; IF NOT EXISTS keeps the migration safe.
ALTER TABLE public.bookings
    ADD COLUMN IF NOT EXISTS cancelled_at timestamptz;

-- --- 2. Indexes -------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_cancellation_token_hash
    ON public.cancellation (cancellation_token_hash)
    WHERE cancellation_token_hash IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_cancellation_booking_id
    ON public.cancellation (booking_id);

CREATE INDEX IF NOT EXISTS idx_bookings_booking_reference
    ON public.bookings (booking_reference);

-- --- 3. Recreate create_public_booking with cancellation insert -------------
-- New argument p_cancellation_token_hash. Failure to insert the cancellation
-- row aborts the same transaction as guest + booking inserts.
DROP FUNCTION IF EXISTS public.create_public_booking(text, text, text, jsonb, jsonb);

CREATE OR REPLACE FUNCTION public.create_public_booking(
    p_idempotency_key           text,
    p_booking_reference         text,
    p_confirmation_token        text,
    p_guest                     jsonb,
    p_bookings                  jsonb,
    p_cancellation_token_hash   text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    v_ref              text;
    v_token            text;
    v_guest_id         public.guests.guest_id%TYPE;
    v_room_id          public.rooms.room_id%TYPE;
    v_first_booking_id public.bookings.booking_id%TYPE;
    v_check_in         date;
    elem               jsonb;
BEGIN
    IF p_idempotency_key IS NULL OR length(p_idempotency_key) < 16 THEN
        RAISE EXCEPTION 'invalid_idempotency_key';
    END IF;
    IF p_bookings IS NULL OR jsonb_array_length(p_bookings) = 0 THEN
        RAISE EXCEPTION 'no_bookings';
    END IF;

    PERFORM pg_advisory_xact_lock(hashtext('gml_book:' || p_idempotency_key));

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

    IF p_cancellation_token_hash IS NULL
       OR p_cancellation_token_hash !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invalid_cancellation_token_hash';
    END IF;

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
        RAISE EXCEPTION 'guest_email_conflict';
    END;

    FOR elem IN SELECT * FROM jsonb_array_elements(p_bookings)
    LOOP
        SELECT r.room_id INTO v_room_id
          FROM public.rooms r
         WHERE r.room_id::text = (elem->>'room_id');
        IF v_room_id IS NULL THEN
            RAISE EXCEPTION 'room_unavailable';
        END IF;

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

    -- v_first_booking_id / v_check_in are overwritten each loop iteration.
    -- Re-select the first row of this reservation so one credential covers
    -- the whole multi-room booking_reference.
    SELECT b.booking_id, b.check_in
      INTO v_first_booking_id, v_check_in
      FROM public.bookings b
     WHERE b.booking_reference = p_booking_reference
     ORDER BY b.booking_id
     LIMIT 1;

    -- Dummy uuid fills the leftover NOT NULL cancellation_token column.
    -- Authorization uses cancellation_token_hash only.
    -- Expiry: lodge check-in time (15:00 America/Edmonton) on the stay date.
    INSERT INTO public.cancellation (
        booking_id,
        cancellation_token,
        cancellation_token_hash,
        token_expiry,
        token_usage,
        token_used_at
    ) VALUES (
        v_first_booking_id,
        gen_random_uuid(),
        p_cancellation_token_hash,
        ((v_check_in + TIME '15:00') AT TIME ZONE 'America/Edmonton'),
        false,
        NULL
    );

    RETURN jsonb_build_object(
        'booking_reference', p_booking_reference,
        'confirmation_token', p_confirmation_token,
        'reused', false
    );
END;
$$;

REVOKE ALL ON FUNCTION public.create_public_booking(text, text, text, jsonb, jsonb, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.create_public_booking(text, text, text, jsonb, jsonb, text)
    TO anon, authenticated, service_role;

-- --- 4. Atomic public cancellation ------------------------------------------
CREATE OR REPLACE FUNCTION public.cancel_public_booking(
    p_booking_reference         text,
    p_cancellation_token_hash   text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public
AS $$
DECLARE
    v_ref              text;
    v_cancel_id        uuid;
    v_usage            boolean;
    v_expiry           timestamptz;
    v_cancelled_count  integer := 0;
BEGIN
    IF p_cancellation_token_hash IS NULL
       OR p_cancellation_token_hash !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invalid_cancellation';
    END IF;
    IF p_booking_reference IS NULL OR length(trim(p_booking_reference)) = 0 THEN
        RAISE EXCEPTION 'invalid_cancellation';
    END IF;

    v_ref := upper(trim(p_booking_reference));
    PERFORM pg_advisory_xact_lock(hashtext('gml_cancel:' || v_ref));

    SELECT c.id, COALESCE(c.token_usage, false), c.token_expiry
      INTO v_cancel_id, v_usage, v_expiry
      FROM public.cancellation c
      JOIN public.bookings b ON b.booking_id = c.booking_id
     WHERE c.cancellation_token_hash = p_cancellation_token_hash
       AND upper(b.booking_reference) = v_ref
     FOR UPDATE OF c;

    IF v_cancel_id IS NULL THEN
        RAISE EXCEPTION 'invalid_cancellation';
    END IF;
    IF v_usage THEN
        RAISE EXCEPTION 'already_used';
    END IF;
    IF v_expiry IS NULL OR v_expiry <= now() THEN
        RAISE EXCEPTION 'expired';
    END IF;

    -- Lock every physical row of this reservation in stable order.
    PERFORM 1
      FROM public.bookings b
     WHERE upper(b.booking_reference) = v_ref
     ORDER BY b.booking_id
     FOR UPDATE OF b;

    IF EXISTS (
        SELECT 1 FROM public.bookings b
         WHERE upper(b.booking_reference) = v_ref
           AND b.booking_status = 'checked_in'
    ) THEN
        RAISE EXCEPTION 'cannot_cancel';
    END IF;

    UPDATE public.bookings b
       SET booking_status = 'cancelled',
           cancelled_at = COALESCE(b.cancelled_at, now())
     WHERE upper(b.booking_reference) = v_ref
       AND b.booking_status NOT IN ('cancelled', 'no_show', 'checked_out');

    GET DIAGNOSTICS v_cancelled_count = ROW_COUNT;

    -- Mark this credential used, plus any sibling rows for the same stay.
    UPDATE public.cancellation c
       SET token_usage = true,
           token_used_at = COALESCE(c.token_used_at, now())
     WHERE c.id = v_cancel_id
        OR c.booking_id IN (
            SELECT b.booking_id FROM public.bookings b
             WHERE upper(b.booking_reference) = v_ref
        );

    RETURN jsonb_build_object(
        'ok', true,
        'booking_reference', v_ref,
        'cancelled_count', v_cancelled_count,
        'booking_status', 'cancelled'
    );
END;
$$;

REVOKE ALL ON FUNCTION public.cancel_public_booking(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.cancel_public_booking(text, text) FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.cancel_public_booking(text, text) TO service_role;

-- --- 5. RLS / grants on public.cancellation ---------------------------------
-- Drop every existing policy (including the dead anon UPDATE).
DO $$
DECLARE
    pol record;
BEGIN
    FOR pol IN
        SELECT policyname
          FROM pg_policies
         WHERE schemaname = 'public'
           AND tablename = 'cancellation'
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.cancellation', pol.policyname);
    END LOOP;
END
$$;

ALTER TABLE public.cancellation ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.cancellation FROM PUBLIC;
REVOKE ALL ON TABLE public.cancellation FROM anon, authenticated;
GRANT ALL ON TABLE public.cancellation TO service_role;

NOTIFY pgrst, 'reload schema';
