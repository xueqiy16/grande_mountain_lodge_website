Grande Mountain Lodge Website & Admin Dashboard PRD

Project Overview

Goal: Build a professional, secure, and functional motel website with:

Online bookings via Moneris Hosted Tokenization

Admin dashboard (React + Supabase) for booking management

Email confirmations via Gmail SMTP

Full database support (Supabase cloud DB)

Free HTTPS with custom domain

Lightweight UI/UX enhancements and page transitions

Privacy and Booking Conditions compliance

Bucketed Architecture
1. Hosting & Domain

Tools: Vercel, GitHub, Let’s Encrypt (built-in to Vercel)
Objective: Deploy website, enable HTTPS, connect custom domain.

Steps:

Create a GitHub repository called grande_mountain_lodge_website.

Push your local folder (grande_mountain_lodge_website) to GitHub.

Sign up / log into Vercel → import GitHub repo.

In Vercel dashboard:

Connect primary domain: grandemountainlodge.com

Connect secondary domain: grandemountainlodge.ca (set up 301 redirect → .com)

Let Vercel automatically enable HTTPS (free via Let’s Encrypt).

Estimated Time: 30–60 minutes

2. Database

Tools: Supabase (cloud PostgreSQL, free tier)
Objective: Secure, persistent storage for bookings, rooms, guests, payment tokens.

Schema Recommendations:

Table: rooms

Column	Type	Notes
room_id	UUID / serial	Primary key
room_number	int	Unique number
type	varchar	STD, STU, STE, OR
status	varchar	Available, booked, under maintenance

Table: bookings

Column	Type	Notes
booking_id	UUID / serial	Primary key
guest_id	UUID	Foreign key → guests
room_id	UUID	Foreign key → rooms
check_in	date	Calendar
check_out	date	Calendar
adults	int	
children	int	
pets	int	Optional
booking_status	varchar	Pending, Confirmed, Checked-in, Cancelled
token	varchar	Moneris token
card_brand	varchar	Visa/MC/Amex
last4	varchar	Last 4 digits
expiry_month	int	
expiry_year	int	

Table: guests

Column	Type	Notes
guest_id	UUID / serial	Primary key
first_name	varchar	
last_name	varchar	
email	varchar	
phone	varchar	
address	text	Street, city, province, postal, country

Security Notes:

Never store card number / CVV. Only store token, last4, expiry, brand.

Use Supabase RLS (row-level security) to restrict access to admin users.

Estimated Time: 1–2 hours for setup and table creation

3. Admin Dashboard (React + Supabase)

Tools: React, Supabase Auth, TailwindCSS for design, Framer Motion for transitions

Features to Implement:

Booking Management: View, edit, cancel bookings

Room Management: Add, edit, remove rooms

Guest Info: View guest details

Payment: Charge guest using Moneris token

Analytics: Basic occupancy and revenue dashboard

Security: Email/password login, JWT tokens, admin-only access

UI Recommendations:

Dashboard layout: sidebar navigation, main content area

Use TailwindCSS for fast, clean styling

Framer Motion for subtle page transitions

Keyboard shortcuts: N=New booking, E=Edit, C=Cancel, P=Payment, V=Folio

Estimated Time: 10–15 hours (split across learning React + Supabase, coding, and testing)

4. Booking Flow

Steps:

Guest clicks Book Now.

Moneris iframe opens for card info.

Guest enters card → Moneris returns:

payment_token

card_brand

last4

expiry_month/year

Store only allowed fields in bookings table.

Optionally perform $0 or $1 authorization.

Email confirmation sent automatically.

Security Notes:

Never log card data.

Only your server + Moneris can use token for charging.

No account creation for guests.

Estimated Time: 3–4 hours for integration and testing

5. Email Confirmation

Tools: Gmail SMTP (reception@grandemountainlodge.com
), Node.js / React backend
Steps:

When booking is successfully created → trigger email.

Include booking details, check-in/out dates, cancellation policy.

Optional: include PDF attachment receipt.

Estimated Time: 1–2 hours

6. Front-End Aesthetics & Transitions

Tools: TailwindCSS, Framer Motion
Steps:

Add lightweight fade/slide transitions between pages.

Add small animations for Book Now button hover, iframe load, etc.

Focus on clean, minimal, professional aesthetic.

Estimated Time: 1–2 hours

7. Privacy Policy & Booking Conditions

Tasks:

Update Privacy Policy (already drafted)

Add Booking Conditions / Terms Page (Canadian-compliant)

Check-in/out times

Cancellation / no-show policy

Payment guarantee explanation

Estimated Time: 1 hour

8. Security & HTTPS

HTTPS handled automatically by Vercel.

Admin dashboard: Supabase Auth + RLS

Sensitive data encrypted

Optional: enable 2FA for admin login

Estimated Time: 30 minutes

9. Deployment Flow

GitHub → Vercel → preview → test → production.

Connect .com primary domain + .ca secondary → redirect to .com.

Verify SSL → site is fully HTTPS.

Set up email SMTP for booking confirmations.

Verify Moneris tokenization workflow.

Estimated Time: 1–2 hours

Full Step-by-Step To-Do List
Task	Time	Steps

Set up GitHub repo & Vercel hosting	30–60 min	Create repo → push local code → import to Vercel → connect domain → HTTPS enabled

Set up Supabase project & DB tables	1–2 hrs	Create rooms, bookings, guests tables → enable RLS → configure admin role

Admin Dashboard Skeleton (React + Supabase Auth)	2–3 hrs	Create React app → connect Supabase → implement login/logout → layout dashboard

Booking Management in Admin	3–4 hrs	View/edit/cancel bookings → integrate Moneris token → link to database

Room Management in Admin	1–2 hrs	Add/edit/delete rooms → link to bookings

Guest Info & Folio	1–2 hrs	Display guest details → optional: print folio / PDF

Moneris Hosted Tokenization Integration	3–4 hrs	Embed iframe → collect token → save allowed info → $0/$1 auth → test

Email Confirmation Integration	1–2 hrs	Use Gmail SMTP → send confirmation email on booking completion

Front-End Transitions & UI Tweaks	1–2 hrs	TailwindCSS + Framer Motion → small animations, hover effects, fade between pages

Privacy Policy & Booking Conditions Pages	1 hr	Update privacy page → generate Canadian-compliant booking conditions

Testing & Security Audit	2–3 hrs	Verify HTTPS → test admin access → test bookings → verify tokenization → check database safety

Final Deployment & Domain Setup	1–2 hrs	Connect .com → .ca redirect → verify SSL → push to production

Total Estimated Time: 17–25 hours (can be split over multiple days)