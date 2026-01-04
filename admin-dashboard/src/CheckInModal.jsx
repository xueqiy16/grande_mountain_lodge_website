import React, { useState, useEffect } from 'react';
import { supabase } from './lib/supabase';

const CheckInModal = ({ isOpen, onClose, booking, onCheckInComplete }) => {
  // Helper function to calculate total balance (Stay Total)
  const calculateTotalBalance = (booking) => {
    if (!booking || !booking.check_in || !booking.check_out || !booking.rooms?.room_types?.nightly_rate) return 0;
    const start = new Date(booking.check_in + "T00:00:00");
    const end = new Date(booking.check_out + "T00:00:00");
    const diffInMs = end.getTime() - start.getTime();
    const diffNights = Math.max(1, Math.ceil(diffInMs / (1000 * 60 * 60 * 24))); 
    return Number(diffNights) * Number(booking.rooms.room_types.nightly_rate);
  };

  const [isProcessing, setIsProcessing] = useState(false);
  const [formData, setFormData] = useState({
    card_brand: 'Visa',
    last4: '',
    expiry_month: '',
    expiry_year: '',
    initial_balance: '0.00'
  });

  // Set initial balance to Stay Total when modal opens
  useEffect(() => {
    if (isOpen && booking) {
      const stayTotal = calculateTotalBalance(booking);
      setFormData(prev => ({
        ...prev,
        initial_balance: stayTotal.toFixed(2)
      }));
    }
  }, [isOpen, booking]);

  // Reset form when modal closes
  useEffect(() => {
    if (!isOpen) {
      setFormData({
        card_brand: 'Visa',
        last4: '',
        expiry_month: '',
        expiry_year: '',
        initial_balance: '0.00'
      });
      setIsProcessing(false);
    }
  }, [isOpen]);

  const handleSubmit = async (e) => {
    e.preventDefault();
    
    if (!booking) return;

    if (!formData.last4 || formData.last4.length !== 4) {
      alert("Please enter the last 4 digits of the card.");
      return;
    }

    if (!formData.expiry_month || !formData.expiry_year) {
      alert("Please enter the card expiry date.");
      return;
    }

    const initialBalance = Number(formData.initial_balance);
    if (isNaN(initialBalance) || initialBalance < 0) {
      alert("Please enter a valid initial balance amount.");
      return;
    }

    setIsProcessing(true);

    try {
      // Update booking with card info, initial balance, and change status to 'Checked in'
      const { error: bookingError } = await supabase
        .from('bookings')
        .update({
          card_brand: formData.card_brand,
          card_last4: formData.last4,
          card_exp_month: parseInt(formData.expiry_month),
          card_exp_year: parseInt(formData.expiry_year),
          amount_paid: 0, // Reset to 0, initial balance will be outstanding
          booking_status: 'Checked in'
        })
        .eq('booking_id', booking.booking_id);

      if (bookingError) {
        throw bookingError;
      }

      // Update room status to 'Occupied'
      const { error: roomError } = await supabase
        .from('rooms')
        .update({ status: 'Occupied' })
        .eq('room_id', booking.room_id);

      if (roomError) {
        throw roomError;
      }

      // Success
      onCheckInComplete(`Checked in successfully.`);
      onClose();
    } catch (error) {
      alert(`Check-in failed: ${error.message}`);
      setIsProcessing(false);
    }
  };

  const handleClose = () => {
    if (isProcessing) return;
    onClose();
  };

  if (!isOpen) return null;

  return (
    <div className="modal-overlay" onClick={handleClose}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>Check-In Guest</h3>
          <button 
            onClick={handleClose} 
            className="close-x"
            disabled={isProcessing}
            style={{
              background: 'transparent',
              border: 'none',
              fontSize: '1.5rem',
              fontWeight: 300,
              color: '#64748b',
              cursor: isProcessing ? 'not-allowed' : 'pointer',
              padding: 0,
              width: '28px',
              height: '28px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              borderRadius: '4px',
              opacity: isProcessing ? 0.5 : 1
            }}
          >
            ✕
          </button>
        </div>
        
        <form onSubmit={handleSubmit} className="walkin-form">
          {booking && (
            <>
              <div className="form-section" style={{ marginBottom: '20px' }}>
                <label>Guest & Room</label>
                <div style={{ 
                  padding: '10px', 
                  background: '#f1f5f9', 
                  border: '1px solid #e2e8f0',
                  borderRadius: '8px',
                  fontWeight: 600
                }}>
                  {booking.guests?.first_name} {booking.guests?.last_name} - Room {booking.rooms?.room_number}
                </div>
              </div>
              
              <div className="form-section" style={{ marginBottom: '20px' }}>
                <label>Outstanding Balance (Stay Total)</label>
                <input 
                  type="number" 
                  step="0.01"
                  min="0"
                  required
                  value={formData.initial_balance}
                  onChange={(e) => setFormData({...formData, initial_balance: e.target.value})}
                  disabled={isProcessing}
                  style={{ 
                    padding: '10px', 
                    background: '#f1f5f9', 
                    border: '1px solid #e2e8f0',
                    borderRadius: '8px',
                    fontWeight: 600,
                    fontSize: '1.1rem'
                  }}
                />
              </div>
            </>
          )}

          <div className="form-section-title">Card Information</div>
          
          <div className="form-grid-3" style={{ gridTemplateColumns: '1fr 1fr 1fr' }}>
            <div className="form-group">
              <label>Card Brand *</label>
              <select 
                required
                value={formData.card_brand}
                onChange={(e) => setFormData({...formData, card_brand: e.target.value})}
                disabled={isProcessing}
              >
                <option value="Visa">Visa</option>
                <option value="Mastercard">Mastercard</option>
                <option value="Amex">Amex</option>
                <option value="Interac">Interac</option>
                <option value="Cash">Cash</option>
              </select>
            </div>
            <div className="form-group">
              <label>Last 4 Digits *</label>
              <input 
                type="text"
                maxLength="4"
                required
                value={formData.last4}
                onChange={(e) => setFormData({...formData, last4: e.target.value.replace(/\D/g, '')})}
                disabled={isProcessing}
                placeholder="1234"
              />
            </div>
            <div className="form-group">
              <label>Expiry Month *</label>
              <input 
                type="number"
                min="1"
                max="12"
                required
                value={formData.expiry_month}
                onChange={(e) => setFormData({...formData, expiry_month: e.target.value})}
                disabled={isProcessing}
                placeholder="MM"
              />
            </div>
          </div>

          <div className="form-grid-3" style={{ gridTemplateColumns: '1fr 2fr' }}>
            <div className="form-group">
              <label>Expiry Year *</label>
              <input 
                type="number"
                min="2024"
                required
                value={formData.expiry_year}
                onChange={(e) => setFormData({...formData, expiry_year: e.target.value})}
                disabled={isProcessing}
                placeholder="YYYY"
              />
            </div>
          </div>

          <button 
            type="submit" 
            className="tool-btn primary" 
            style={{ width: '100%', marginTop: '20px' }}
            disabled={isProcessing}
          >
            {isProcessing ? 'Processing...' : 'Complete Check-In'}
          </button>
        </form>
      </div>
    </div>
  );
};

export default CheckInModal;

