import React, { useState, useEffect } from 'react';
import { supabase } from './lib/supabase';

const WalkInModal = ({ isOpen, onClose, availableRooms, onBookingComplete }) => {
  const [roomTypes, setRoomTypes] = useState([]);
  const [selectedType, setSelectedType] = useState('');
  const [formData, setFormData] = useState({
    first_name: '', last_name: '', email: '', phone: '', address: '', city: '', country: '',
    check_in: new Date().toISOString().split('T')[0], // Default to today
    check_out: '', adults: 1, children: 0, pets: 0,
    card_brand: 'Visa', last4: '', expiry_month: '', expiry_year: ''
  });

  const isCashOrDebit = formData.card_brand === 'Cash' || formData.card_brand === 'Debit';

  useEffect(() => {
    const fetchTypes = async () => {
      const { data } = await supabase.from('room_types').select('*');
      if (data) setRoomTypes(data);
    };
    if (isOpen) fetchTypes();
  }, [isOpen]);

  const handleSubmit = async (e) => {
    e.preventDefault();

    const targetRoom = availableRooms.find(r => r.room_type_id === selectedType);
    if (!targetRoom) return alert("No rooms available for this type!");

    const today = new Date().toISOString().split('T')[0];
    // LOGIC: If check_in is future, status is 'Reserved'. If today, 'Checked in'.
    const isFutureBooking = formData.check_in > today;
    const finalStatus = isFutureBooking ? 'Reserved' : 'Checked in';

    // 1. Create Guest
    const { data: guestData, error: guestError } = await supabase
      .from('guests')
      .insert([{ 
        first_name: formData.first_name, last_name: formData.last_name,
        email: formData.email, phone: formData.phone,
        address: formData.address, city: formData.city, country: formData.country
      }])
      .select().single();

    if (guestError) return alert("Guest Error: " + guestError.message);

    // 2. Create Booking
    const { error: bookingError } = await supabase
      .from('bookings')
      .insert([{
        guest_id: guestData.guest_id,
        room_id: targetRoom.room_id,
        check_in: formData.check_in,
        check_out: formData.check_out,
        adults: formData.adults,
        children: formData.children,
        pets: formData.pets,
        booking_status: finalStatus,
        token: `RES-${Math.random().toString(36).substring(2, 8).toUpperCase()}`,
        card_brand: formData.card_brand,
        last4: isCashOrDebit ? null : formData.last4,
        expiry_month: isCashOrDebit ? null : parseInt(formData.expiry_month) || null,
        expiry_year: isCashOrDebit ? null : parseInt(formData.expiry_year) || null
      }]);

    if (bookingError) return alert("Booking Error: " + bookingError.message);

    // 3. Update Room Status ONLY if checking in today
    if (!isFutureBooking) {
      await supabase.from('rooms').update({ status: 'Occupied' }).eq('room_id', targetRoom.room_id);
    }

    onBookingComplete(isFutureBooking ? `Reservation created for Room ${targetRoom.room_number}` : `Checked into Room ${targetRoom.room_number}!`);
    onClose();
  };

  if (!isOpen) return null;

  return (
    <div className="modal-overlay">
      <div className="modal-content walkin-modal-wide">
        <div className="modal-header">
          <h3>New Reservation</h3>
          <button onClick={onClose} className="close-x">✕</button>
        </div>
        
        <form onSubmit={handleSubmit} className="walkin-form">
          <div className="form-section" style={{ marginBottom: '20px' }}>
            <label>1. Select Room Category</label>
            <select required value={selectedType} onChange={(e) => setSelectedType(e.target.value)}>
              <option value="">Select Room Type...</option>
              {roomTypes.map(t => (
                <option key={t.room_type_id} value={t.room_type_id}>{t.name} (${t.nightly_rate})</option>
              ))}
            </select>
          </div>

          {/* Identity Group */}
          <div className="form-grid-3">
            <div className="form-group"><label>First Name</label><input type="text" required onChange={(e) => setFormData({...formData, first_name: e.target.value})} /></div>
            <div className="form-group"><label>Last Name</label><input type="text" required onChange={(e) => setFormData({...formData, last_name: e.target.value})} /></div>
            <div className="form-group"><label>Email</label><input type="email" required onChange={(e) => setFormData({...formData, email: e.target.value})} /></div>
          </div>

          {/* Schedule Group */}
          <div className="form-grid-3">
            <div className="form-group"><label>Check-In Date</label><input type="date" value={formData.check_in} required onChange={(e) => setFormData({...formData, check_in: e.target.value})} /></div>
            <div className="form-group"><label>Check-Out Date</label><input type="date" required onChange={(e) => setFormData({...formData, check_out: e.target.value})} /></div>
            <div className="form-group"><label>Phone</label><input type="text" required onChange={(e) => setFormData({...formData, phone: e.target.value})} /></div>
          </div>

          {/* Location Group */}
          <div className="form-grid-3">
            <div className="form-group"><label>Address</label><input type="text" onChange={(e) => setFormData({...formData, address: e.target.value})} /></div>
            <div className="form-group"><label>City</label><input type="text" onChange={(e) => setFormData({...formData, city: e.target.value})} /></div>
            <div className="form-group"><label>Country</label><input type="text" onChange={(e) => setFormData({...formData, country: e.target.value})} /></div>
          </div>

          <div className="form-grid-3">
            <div className="form-group"><label>Adults/Children/Pets</label>
              <div style={{display:'flex', gap:'5px'}}>
                <input type="number" placeholder="A" value={formData.adults} onChange={(e) => setFormData({...formData, adults: e.target.value})} />
                <input type="number" placeholder="C" value={formData.children} onChange={(e) => setFormData({...formData, children: e.target.value})} />
                <input type="number" placeholder="P" value={formData.pets} onChange={(e) => setFormData({...formData, pets: e.target.value})} />
              </div>
            </div>
          </div>

          <div className="form-section-title">Payment Details</div>
          <div className="form-grid-3">
            <div className="form-group">
              <label>Card Brand</label>
              <select value={formData.card_brand} onChange={(e) => setFormData({...formData, card_brand: e.target.value})}>
                <option>Visa</option><option>Mastercard</option><option>AMEX</option><option>Debit</option><option>Cash</option>
              </select>
            </div>
            <div className="form-group">
              <label>Last 4 Digits{!isCashOrDebit && ' *'}</label>
              <input 
                type="text" 
                maxLength="4" 
                value={formData.last4}
                onChange={(e) => setFormData({...formData, last4: e.target.value})}
                disabled={isCashOrDebit}
                required={!isCashOrDebit}
                style={{ 
                  backgroundColor: isCashOrDebit ? '#f1f5f9' : 'white',
                  color: isCashOrDebit ? '#94a3b8' : 'inherit',
                  cursor: isCashOrDebit ? 'not-allowed' : 'text'
                }}
              />
            </div>
            <div className="form-group">
              <label>Expiry (MM/YYYY){!isCashOrDebit && ' *'}</label>
              <div style={{display:'flex', gap:'5px'}}>
                <input 
                  type="number" 
                  placeholder="MM" 
                  value={formData.expiry_month}
                  onChange={(e) => setFormData({...formData, expiry_month: e.target.value})}
                  disabled={isCashOrDebit}
                  required={!isCashOrDebit}
                  style={{ 
                    backgroundColor: isCashOrDebit ? '#f1f5f9' : 'white',
                    color: isCashOrDebit ? '#94a3b8' : 'inherit',
                    cursor: isCashOrDebit ? 'not-allowed' : 'text'
                  }}
                />
                <input 
                  type="number" 
                  placeholder="YYYY" 
                  value={formData.expiry_year}
                  onChange={(e) => setFormData({...formData, expiry_year: e.target.value})}
                  disabled={isCashOrDebit}
                  required={!isCashOrDebit}
                  style={{ 
                    backgroundColor: isCashOrDebit ? '#f1f5f9' : 'white',
                    color: isCashOrDebit ? '#94a3b8' : 'inherit',
                    cursor: isCashOrDebit ? 'not-allowed' : 'text'
                  }}
                />
              </div>
            </div>
          </div>

          <button type="submit" className="tool-btn primary" style={{ width: '100%', marginTop: '20px' }}>
            Complete Reservation
          </button>
        </form>
      </div>
    </div>
  );
};

export default WalkInModal;