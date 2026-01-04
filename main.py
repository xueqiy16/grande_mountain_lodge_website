import sqlite3
from flask import Flask, render_template, request, redirect
from datetime import datetime

app = Flask(__name__)

# All rooms
"""
LEGEND - ROOM CLASSIFICATIONS:
STD: Standard - One room, no kitchen
STU: Studio   - One room + kitchenette
STE: Suite    - Separate bedroom OR open-plan premium
"""

rooms = [
    # Room Number, Specific Name, Room Type
    #Standard Queen Non-Smoking (1 Room)
    {"no": "225", "name": "Standard Queen Non-Smoking", "type": "STD-Q-NS"},
    
    # Studio Queen Non-Smoking (11 Rooms)
    {"no": "105", "name": "Studio Queen Non-Smoking", "type": "STU-Q-NS"},
    {"no": "113", "name": "Studio Queen Non-Smoking", "type": "STU-Q-NS"},
    {"no": "116", "name": "Studio Queen Non-Smoking", "type": "STU-Q-NS"},
    {"no": "122", "name": "Studio Queen Non-Smoking", "type": "STU-Q-NS"},
    {"no": "123", "name": "Studio Queen Non-Smoking", "type": "STU-Q-NS"},
    {"no": "207", "name": "Studio Queen Non-Smoking", "type": "STU-Q-NS"},
    {"no": "210", "name": "Studio Queen Non-Smoking", "type": "STU-Q-NS"},
    {"no": "212", "name": "Studio Queen Non-Smoking", "type": "STU-Q-NS"},
    {"no": "213", "name": "Studio Queen Non-Smoking", "type": "STU-Q-NS"},
    {"no": "219", "name": "Studio Queen Non-Smoking", "type": "STU-Q-NS"},
    {"no": "222", "name": "Studio Queen Non-Smoking", "type": "STU-Q-NS"},

    # Studio Double Queen Non-Smoking (19 Rooms)
    {"no": "101", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "102", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "103", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "108", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "109", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "111", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "112", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "114", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "118", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "120", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "209", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "211", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "214", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "215", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "217", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "218", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "220", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "221", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},
    {"no": "223", "name": "Studio Double Queen Non-Smoking", "type": "STU-QQ-NS"},

    # Suite Queen Non-Smoking (2 Rooms)
    {"no": "216", "name": "Suite Queen Non-Smoking", "type": "STE-Q-NS"},
    {"no": "224", "name": "Suite Queen Non-Smoking", "type": "STE-Q-NS"},

    # Suite King Non-Smoking (1 Room)
    {"no": "227", "name": "Suite King Non-Smoking", "type": "STE-K-NS"},

    # Studio Queen Smoking (1 Room)
    {"no": "205", "name": "Studio Queen Smoking", "type": "STU-Q-SM"},

    # Studio Double Queen Smoking (3 Rooms)
    {"no": "202", "name": "Studio Double Queen Smoking", "type": "STU-QQ-SM"},
    {"no": "203", "name": "Studio Double Queen Smoking", "type": "STU-QQ-SM"},
    {"no": "208", "name": "Studio Double Queen Smoking", "type": "STU-QQ-SM"}
]

# Define room prices
ROOM_PRICES = {
    "Classic Queen Smoking": 109.00,
    "Classic Queen Non-Smoking": 119.00,
    "Double Queen Smoking": 139.00,
    "Double Queen Non-Smoking": 149.00
}

# This creates the database file and the table if they don't exist
def init_db():
    conn = sqlite3.connect('bookings.db')
    db = conn.cursor()
    db.execute('''
        CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_type TEXT,
            checkin TEXT,
            checkout TEXT,
            adults INTEGER,
            children INTEGER,
            pets INTEGER,
            total_price REAL
        )
    ''')
    conn.commit()
    conn.close()

init_db()

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/travel-guide')
def travel_guide():
    return render_template('travel-guide.html')

@app.route('/booking')
def booking():
    return render_template('booking.html')

@app.route('/contact')
def contact():
    return render_template('contact.html')

@app.route('/booker_contact')
def booker_contact():
    return render_template('booker_contact.html')

@app.route('/confirm-booking', methods=['POST'])
def handle_booking():
    # 1. Capture all data from the form
    room = request.form.get('room_selection')
    start_str = request.form.get('start_date')
    end_str = request.form.get('end_date')
    
    # SAFETY CHECK: If dates are missing, send them back or show an error
    if not start_str or not end_str:
        return "<h3>Error: Please go back and select check-in and check-out dates.</h3>"
    
    # Capture the counts from the hidden inputs
    adults = request.form.get('adult_count')
    kids = request.form.get('child_count')
    pets = request.form.get('pet_count')
    
    # 2. Calculate the number of nights
    # strptime turns the text "2025-12-21" into a Python date object
    start_date = datetime.strptime(start_str, '%Y-%m-%d')
    end_date = datetime.strptime(end_str, '%Y-%m-%d')
    nights = (end_date - start_date).days
    
    # Ensure nights is at least 1 to avoid $0 charges
    if nights <= 0:
        nights = 1
    
    # 3. Calculate Total Price
    # This looks at the dictionary we created earlier
    price_per_night = ROOM_PRICES.get(room, 100.00)
    total = price_per_night * nights

    # 4. Save everything to the Database (7 columns total)
    conn = sqlite3.connect('bookings.db')
    db = conn.cursor()
    db.execute('''
        INSERT INTO reservations (room_type, checkin, checkout, adults, children, pets, total_price)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (room, start_str, end_str, adults, kids, pets, total))
    conn.commit()
    conn.close()

    return f"""
    <h1>Booking Saved!</h1>
    <p>We have recorded your stay for the <strong>{room}</strong>.</p>
    <p>Total for {nights} night(s): <strong>${total:.2f}</strong></p>
    <a href='/'>Back to Home</a>
    """

@app.route('/admin-dashboard')
def admin_dashboard():
    init_db()
    conn = sqlite3.connect('bookings.db')
    db = conn.cursor()
    
    # 1. Get all active bookings
    db.execute("SELECT * FROM reservations")
    all_bookings = db.fetchall()
    
    # Create a list of currently occupied room types
    occupied_room_types = [b[1] for b in all_bookings]
    
    # 2. Define your 10 physical rooms and their types
    # In a real motel, you'd have room numbers
    rooms = [
        {"no": "101", "type": "Classic Queen Smoking"},
        {"no": "102", "type": "Classic Queen Non-Smoking"},
        {"no": "103", "type": "Double Queen Smoking"},
        {"no": "104", "type": "Double Queen Non-Smoking"},
        {"no": "105", "type": "Classic Queen Smoking"},
        {"no": "201", "type": "Classic Queen Non-Smoking"},
        {"no": "202", "type": "Double Queen Smoking"},
        {"no": "203", "type": "Double Queen Non-Smoking"},
        {"no": "204", "type": "Maintenance"}, # Manually set one to maintenance
        {"no": "301", "type": "Classic Queen Non-Smoking"}
    ]

    # 3. Logic to assign status
    for room in rooms:
        if room["type"] == "Maintenance":
            room["status"] = "maintenance"
        elif room["type"] in occupied_room_types:
            room["status"] = "occupied"
            # Remove from list so the next room of same type shows as available
            occupied_room_types.remove(room["type"])
        else:
            room["status"] = "available"

    # 4. Calculate Stats
    total_revenue = sum(b[7] for b in all_bookings)
    available_count = sum(1 for r in rooms if r["status"] == "available")
    occupancy_rate = (len(all_bookings) / 10) * 100

    conn.close()
    return render_template('admin.html', 
        bookings=all_bookings, 
        revenue=total_revenue,
        rooms=rooms,
        occupancy=occupancy_rate,
        available=available_count
    )

@app.route('/delete-booking/<int:booking_id>')
def delete_booking(booking_id):
    conn = sqlite3.connect('bookings.db')
    db = conn.cursor()
    # Delete the specific row using its ID
    db.execute("DELETE FROM reservations WHERE id = ?", (booking_id,))
    conn.commit()
    conn.close()
    
    # Send them back to the dashboard to see it's gone
    return redirect('/admin-dashboard')

@app.route('/elements.html')
def elements():
    return render_template('elements.html')

@app.route('/generic.html')
def generic():
    return render_template('generic.html')

@app.route('/rooms')
def rooms():
    # This renders the rooms.html file located in your templates folder
    return render_template('rooms.html')

@app.route('/final_details')
def final_details():
    return render_template('final_details.html')

@app.route('/privacy-policy')
def privacy_policy():
    return render_template('private_policy.html')

@app.route('/terms-and-conditions')
def terms_and_conditions():
    return render_template('terms_and_conditions.html')

if __name__ == '__main__':
    app.run(debug=True, port=5001)