import React, { useState, useEffect } from 'react';
import { supabase } from './lib/supabase';
import Sidebar from './Sidebar';
import './App.css';
import Login from './Login';
import WalkInModal from './WalkInModal';
import PaymentModal from './PaymentModal';
import CheckInModal from './CheckInModal';

function App() {
  const [session, setSession] = useState(null);
  const [rooms, setRooms] = useState([]);
  const [bookings, setBookings] = useState([]);
  const [loading, setLoading] = useState(true);
  const [currentTab, setCurrentTab] = useState('In-House');
  const [selectedRoom, setSelectedRoom] = useState(null);
  const [selectedFolioId, setSelectedFolioId] = useState(null);
  const activeFolio = bookings.find(b => b.booking_id === selectedFolioId);
  const [searchTerm, setSearchTerm] = useState('');
  const [isWalkInOpen, setIsWalkInOpen] = useState(false);
  const [isPaymentModalOpen, setIsPaymentModalOpen] = useState(false);
  const [isCheckInModalOpen, setIsCheckInModalOpen] = useState(false);
  const [selectedPaymentBooking, setSelectedPaymentBooking] = useState(null);
  const [selectedCheckInBooking, setSelectedCheckInBooking] = useState(null);
  const [paymentModalContext, setPaymentModalContext] = useState(null); // 'checkin' | 'payment' | null
  const [folioTransactions, setFolioTransactions] = useState([]);
  const [allTransactions, setAllTransactions] = useState([]);
  const [message, setMessage] = useState('');
  const [arrivalDate, setArrivalDate] = useState(new Date().toISOString().split('T')[0]);
  const [departureDate, setDepartureDate] = useState(new Date().toISOString().split('T')[0]);
  const [paymentAmount, setPaymentAmount] = useState('');
  const [profileDropdownOpen, setProfileDropdownOpen] = useState(false);

  useEffect(() => {
    supabase.auth.getSession().then(({ data: { session } }) => {
      setSession(session);
      if (session) fetchDashboardData();
    });
    const { data: { subscription } } = supabase.auth.onAuthStateChange((_event, session) => {
      setSession(session);
      if (session) fetchDashboardData();
    });
    return () => subscription.unsubscribe();
  }, []);

  useEffect(() => {
    if (message) {
      const timer = setTimeout(() => setMessage(''), 3000);
      return () => clearTimeout(timer);
    }
  }, [message]);

  // Fetch transactions when folio is opened
  useEffect(() => {
    const fetchFolioTransactions = async () => {
      if (selectedFolioId) {
        const { data, error } = await supabase
          .from('transactions')
          .select('*')
          .eq('booking_id', selectedFolioId)
          .order('created_at', { ascending: true });
        
        if (!error && data) {
          setFolioTransactions(data);
        }
      } else {
        setFolioTransactions([]);
      }
    };
    
    fetchFolioTransactions();
  }, [selectedFolioId]);

  // Auto-close drawer when tab changes
  useEffect(() => {
    setSelectedRoom(null);
  }, [currentTab]);

  const fetchDashboardData = async () => {
    setLoading(true);
    const [roomsRes, bookingsRes, transactionsRes] = await Promise.all([
      supabase.from('rooms').select('*, room_types(*)').order('room_number', { ascending: true }),
      supabase.from('bookings').select('*, guests(*), rooms(*, room_types(*))'),
      supabase.from('transactions').select('*')
    ]);
    if (roomsRes.data) setRooms(roomsRes.data);
    if (bookingsRes.data) setBookings(bookingsRes.data);
    if (transactionsRes.data) setAllTransactions(transactionsRes.data);
    setLoading(false);
  };

  const activeBooking = selectedRoom 
    ? bookings.find(b => 
        b.room_id === selectedRoom.room_id && 
        (b.booking_status === 'Checked in' || b.booking_status === 'Reserved')
      ) 
    : null;

  // Primary Status Helper - determines the single primary status for a room
  const getPrimaryStatus = (room, bookings) => {
    const activeB = bookings.find(b => b.room_id === room.room_id);
    if (activeB?.booking_status === 'Checked in') return 'Occupied';
    if (activeB?.booking_status === 'Reserved') return 'Reserved';
    if (room.status === 'Dirty') return 'Dirty';
    return 'Available';
  };

  const calculateTotalBalance = (booking) => {
    if (!booking || !booking.check_in || !booking.check_out || !booking.rooms?.room_types?.nightly_rate) return 0;
    const start = new Date(booking.check_in + "T00:00:00");
    const end = new Date(booking.check_out + "T00:00:00");
    const diffInMs = end.getTime() - start.getTime();
    const diffNights = Math.max(1, Math.ceil(diffInMs / (1000 * 60 * 60 * 24))); 
    return Number(diffNights) * Number(booking.rooms.room_types.nightly_rate);
  };

  const calculateOutstandingBalance = (booking) => {
    if (!booking) return "0.00";
    const totalCost = Number(calculateTotalBalance(booking));
    const paid = Number(booking.amount_paid || 0);
    const incidentals = Number(booking.incidentals || 0);
    return (totalCost + incidentals - paid).toFixed(2);
  };

  // Helper function to normalize dates to YYYY-MM-DD format
  const normalizeDate = (dateString) => {
    if (!dateString) return null;
    // If it's already in YYYY-MM-DD format, return as is
    if (typeof dateString === 'string' && dateString.match(/^\d{4}-\d{2}-\d{2}$/)) {
      return dateString;
    }
    // Otherwise, parse it and extract the date portion
    try {
      const date = new Date(dateString);
      if (isNaN(date.getTime())) return null;
      return date.toISOString().split('T')[0];
    } catch (e) {
      return null;
    }
  };

  const handleCheckIn = (booking) => {
    // Open Check-In modal for card info
    setSelectedCheckInBooking(booking);
    setIsCheckInModalOpen(true);
  };

  const handleCheckInComplete = async (msg) => {
    setMessage(msg);
    await fetchDashboardData();
    setIsCheckInModalOpen(false);
    setSelectedCheckInBooking(null);
  };

  const handlePostCharge = async (bookingId, currentIncidentals) => {
    const charge = prompt("Enter the charge amount (e.g., 50.00 for damages):");
    if (!charge || isNaN(charge)) return;
    const newIncidentals = Number(currentIncidentals || 0) + Number(charge);
    const { error } = await supabase.from('bookings').update({ incidentals: newIncidentals }).eq('booking_id', bookingId);
    if (!error) { setMessage(`Charge of $${Number(charge).toFixed(2)} added.`); fetchDashboardData(); }
  };

  const handleLateCheckOut = async (booking) => {
    const lateFee = prompt("Enter late check-out fee amount (e.g., 25.00):");
    if (!lateFee || isNaN(lateFee) || Number(lateFee) <= 0) {
      if (lateFee) alert("Please enter a valid fee amount greater than 0.");
      return;
    }
    
    const currentIncidentals = Number(booking.incidentals || 0);
    const newIncidentals = currentIncidentals + Number(lateFee);
    
    try {
      const { error } = await supabase
        .from('bookings')
        .update({ incidentals: newIncidentals })
        .eq('booking_id', booking.booking_id);
      
      if (!error) {
        setMessage(`Late check-out fee of $${Number(lateFee).toFixed(2)} added.`);
        await fetchDashboardData();
      } else {
        alert("Failed to add late fee: " + error.message);
      }
    } catch (error) {
      alert("Error: " + error.message);
    }
  };

  const handlePostPayment = async (bookingId, currentPaid, amount) => {
    const finalAmount = amount || paymentAmount;
    if (!finalAmount || isNaN(finalAmount)) return alert("Enter a valid numeric amount.");
    const newTotal = parseFloat(currentPaid || 0) + parseFloat(finalAmount);
    const { error } = await supabase.from('bookings').update({ amount_paid: newTotal }).eq('booking_id', bookingId);
    if (!error) { setMessage(`Payment recorded.`); setPaymentAmount(''); fetchDashboardData(); }
  };

  const handleOpenPaymentModal = (booking) => {
    setSelectedPaymentBooking(booking);
    setPaymentModalContext('payment');
    setIsPaymentModalOpen(true);
  };

  const handlePaymentComplete = async (msg) => {
    setMessage(msg);
    
    // If this was a check-in flow, complete the check-in process
    if (paymentModalContext === 'checkin' && selectedPaymentBooking) {
      try {
        await supabase.from('bookings').update({ booking_status: 'Checked in' }).eq('booking_id', selectedPaymentBooking.booking_id);
        await supabase.from('rooms').update({ status: 'Occupied' }).eq('room_id', selectedPaymentBooking.room_id);
        setMessage(`Checked in successfully. ${msg}`);
      } catch (error) {
        alert("Check-in failed: " + error.message);
        return;
      }
    }
    
    // Refresh bookings and transactions
    await fetchDashboardData();
    
    // Refresh transactions for current folio
    if (selectedFolioId) {
      const { data, error } = await supabase
        .from('transactions')
        .select('*')
        .eq('booking_id', selectedFolioId)
        .order('created_at', { ascending: true });
      
      if (!error && data) {
        setFolioTransactions(data);
      }
    }
    
    // Reset payment modal context
    setPaymentModalContext(null);
  };

  const handleCheckOut = async (targetBooking = null) => {
    const bookingToProcess = targetBooking || activeBooking;
    if (!bookingToProcess) return;
    const balance = Number(calculateOutstandingBalance(bookingToProcess));
    if (balance > 0) {
      alert(`Settlement Required: This guest has an outstanding balance of $${balance.toFixed(2)}. Please record a payment before checking out.`);
      setSelectedFolioId(bookingToProcess.booking_id);
      return;
    }
    try {
      await supabase.from('bookings').update({ booking_status: 'Checked out' }).eq('booking_id', bookingToProcess.booking_id);
      await supabase.from('rooms').update({ status: 'Dirty' }).eq('room_id', bookingToProcess.room_id);
      setMessage(`Check-out complete.`);
      setSelectedRoom(null); fetchDashboardData(); 
    } catch (error) { alert("Check-out failed."); }
  };

  const handleCancelReservation = async (bookingId) => {
    if (!window.confirm("Cancel this reservation?")) return;
    const booking = bookings.find(b => b.booking_id === bookingId);
    if (booking?.room_id) {
      // Update room status back to Available
      await supabase.from('rooms').update({ status: 'Available' }).eq('room_id', booking.room_id);
    }
    // Delete the booking (or set status to Cancelled)
    await supabase.from('bookings').delete().eq('booking_id', bookingId);
    setMessage("Reservation cancelled.");
    setSelectedRoom(null); fetchDashboardData();
  };

  const handleMarkClean = async () => {
    if (!selectedRoom || selectedRoom.status !== 'Dirty') return;
    await supabase.from('rooms').update({ status: 'Available' }).eq('room_id', selectedRoom.room_id);
    setMessage(`Room ${selectedRoom.room_number} is ready.`);
    setSelectedRoom(null); fetchDashboardData();
  };

  if (!session) return <Login onLogin={(s) => setSession(s)} />;
  if (loading) return <div className="loading-screen">Loading PMS...</div>;

  return (
    <div className="admin-layout">
      <header className="top-header">
        <div className="header-brand">
          <img src="/assets/logo.png" alt="Grande Mountain Lodge" className="header-logo" />
          <h1>Grande Mountain Lodge</h1>
        </div>
        <div className="header-search-container">
          <input type="text" placeholder="Search by Room # or Guest Name..." value={searchTerm} onChange={(e) => setSearchTerm(e.target.value)} />
          <button onClick={() => setIsWalkInOpen(true)} className="tool-btn primary" style={{ marginLeft: '15px' }}>+ New Reservation</button>
        </div>
        <div className="header-profile-container">
          <div className="header-profile" onClick={() => setProfileDropdownOpen(!profileDropdownOpen)}>
            <div style={{ fontSize: '0.85rem', fontWeight: 600 }}>Zhu Ying</div>
            <div className="profile-avatar">Z</div>
          </div>
          {profileDropdownOpen && (
            <div className="profile-dropdown">
              <button 
                className="profile-dropdown-item" 
                onClick={() => {
                  supabase.auth.signOut();
                  setProfileDropdownOpen(false);
                }}
              >
                Logout
              </button>
            </div>
          )}
        </div>
      </header>

      <Sidebar currentTab={currentTab} setTab={setCurrentTab} />

      <main className="main-content">
        {currentTab === 'Inventory' ? (
          <div className="folio-view">
            <div className="view-header">
              <h2>Inventory & Maintenance Manager</h2>
            </div>
            <div style={{ marginBottom: '30px' }}>
              <h3 style={{ marginBottom: '15px', color: '#64748b', fontSize: '0.9rem', textTransform: 'uppercase' }}>All Rooms</h3>
              <table className="pms-table">
                <thead>
                  <tr>
                    <th>Room Number</th>
                    <th>Room Type</th>
                    <th>Current Status</th>
                    <th>Maintenance Toggle</th>
                  </tr>
                </thead>
                <tbody>
                  {rooms.map(room => {
                    const primaryStatus = getPrimaryStatus(room, bookings);
                    const statusClass = primaryStatus.toLowerCase().replace(' ', '-');
                    return (
                      <tr key={room.room_id}>
                        <td><strong>{room.room_number}</strong></td>
                        <td>{room.room_types?.name}</td>
                        <td><span className={`status-badge status-${statusClass}`}>{primaryStatus}</span></td>
                        <td>
                          {(room.status === 'Available' || room.status === 'Out Of Service') ? (
                          <button
                            onClick={async () => {
                              const newStatus = room.status === 'Available' ? 'Out Of Service' : 'Available';
                              const { error } = await supabase
                                .from('rooms')
                                .update({ status: newStatus })
                                .eq('room_id', room.room_id);
                              if (!error) {
                                setMessage(`Room ${room.room_number} ${newStatus === 'Out Of Service' ? 'set to Out Of Service' : 'restored to Available'}.`);
                                fetchDashboardData();
                              } else {
                                alert("Failed to update room status: " + error.message);
                              }
                            }}
                            className="tool-btn"
                            style={{ fontSize: '0.85rem', padding: '6px 12px' }}
                          >
                            {room.status === 'Available' ? 'Set Out Of Service' : 'Restore to Available'}
                          </button>
                          ) : (
                            <span style={{ color: '#94a3b8', fontSize: '0.85rem' }}>N/A (Room is {primaryStatus})</span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div>
              <h3 style={{ marginBottom: '15px', color: '#64748b', fontSize: '0.9rem', textTransform: 'uppercase' }}>Housekeeping Tracker (Dirty Rooms)</h3>
              {rooms.filter(r => r.status === 'Dirty').length > 0 ? (
                <table className="pms-table">
                  <thead>
                    <tr>
                      <th>Room Number</th>
                      <th>Room Type</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rooms.filter(r => r.status === 'Dirty').map(room => (
                      <tr key={room.room_id}>
                        <td><strong>{room.room_number}</strong></td>
                        <td>{room.room_types?.name}</td>
                        <td><span className="status-badge status-dirty">Dirty</span></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <div className="empty-view">No dirty rooms at this time.</div>
              )}
            </div>
          </div>
        ) : currentTab === 'Daily Audit' ? (
          <div className="folio-view">
            <div className="view-header">
              <h2>Daily Audit Dashboard</h2>
              <div className="pms-stats">
                <div className="stat-pill">
                  Total Stay Revenue (In-House): <span style={{color: '#22c55e'}}>$
                    {Number(bookings
                      .filter(b => b.booking_status === 'Checked in')
                      .reduce((acc, b) => acc + Number(calculateTotalBalance(b)), 0)).toFixed(2)}
                  </span>
                </div>
                <div className="stat-pill">
                  Payments Collected (Today): <span style={{color: '#22c55e'}}>$
                    {Number(allTransactions
                      .filter(t => {
                        if (t.transaction_type !== 'Payment') return false;
                        const today = new Date().toISOString().split('T')[0];
                        const txnDate = normalizeDate(t.created_at);
                        return txnDate === today;
                      })
                      .reduce((acc, t) => acc + Number(t.amount || 0), 0)).toFixed(2)}
                  </span>
                </div>
              </div>
            </div>
            <div style={{ marginBottom: '30px' }}>
              <h3 style={{ marginBottom: '15px', color: '#64748b', fontSize: '0.9rem', textTransform: 'uppercase' }}>Pending Check-Outs (Today)</h3>
              {(() => {
                const today = new Date().toISOString().split('T')[0];
                const pendingCheckouts = bookings.filter(b => 
                  normalizeDate(b.check_out) === today && b.booking_status === 'Checked in'
                );
                return pendingCheckouts.length > 0 ? (
                  <table className="pms-table">
                    <thead>
                      <tr>
                        <th>Guest Name</th>
                        <th>Room</th>
                        <th>Outstanding Balance</th>
                        <th>Action</th>
                      </tr>
                    </thead>
                    <tbody>
                      {pendingCheckouts.map(booking => (
                        <tr key={booking.booking_id}>
                          <td><strong>{booking.guests?.first_name} {booking.guests?.last_name}</strong></td>
                          <td>{booking.rooms?.room_number}</td>
                          <td style={{ color: '#ef4444' }}>${Number(calculateOutstandingBalance(booking)).toFixed(2)}</td>
                          <td>
                            <button onClick={() => handleCheckOut(booking)} className="tool-btn">
                              Check-Out
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <div className="empty-view">No pending check-outs for today.</div>
                );
              })()}
            </div>
            <div>
              <h3 style={{ marginBottom: '15px', color: '#64748b', fontSize: '0.9rem', textTransform: 'uppercase' }}>No-Shows (Expected Today)</h3>
              {(() => {
                const today = new Date().toISOString().split('T')[0];
                const noShows = bookings.filter(b => 
                  normalizeDate(b.check_in) === today && b.booking_status === 'Reserved'
                );
                return noShows.length > 0 ? (
                  <table className="pms-table">
                    <thead>
                      <tr>
                        <th>Guest Name</th>
                        <th>Room</th>
                        <th>Stay Dates</th>
                        <th>Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {noShows.map(booking => (
                        <tr key={booking.booking_id}>
                          <td><strong>{booking.guests?.first_name} {booking.guests?.last_name}</strong></td>
                          <td>{booking.rooms?.room_number}</td>
                          <td>{booking.check_in} to {booking.check_out}</td>
                          <td><span className="status-badge status-reserved">Reserved</span></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <div className="empty-view">No no-shows for today.</div>
                );
              })()}
            </div>
          </div>
        ) : currentTab === 'Guest Folio' ? (
          <div className="folio-view">
            <div className="view-header">
              <h2>Guest Folios & Ledger</h2>
              <div className="pms-stats">
                <div className="stat-pill">Receivables: <span style={{color: '#ef4444'}}>${Number(bookings.reduce((acc, b) => acc + parseFloat(calculateOutstandingBalance(b)), 0)).toFixed(2)}</span></div>
              </div>
            </div>
            <table className="pms-table">
              <thead>
                <tr>
                  <th>Folio ID</th>
                  <th>Guest Name</th>
                  <th>Room</th>
                  <th>Status</th>
                  <th>Balance</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {bookings.filter(b => 
                  `${b.guests?.first_name} ${b.guests?.last_name}`.toLowerCase().includes(searchTerm.toLowerCase()) ||
                  b.rooms?.room_number.toString().includes(searchTerm)
                ).map(b => (
                  <tr key={b.booking_id}>
                    <td className="folio-number">#FL-{b.booking_id.toString().slice(-5)}</td>
                    <td><strong>{b.guests?.first_name} {b.guests?.last_name}</strong></td>
                    <td>{b.rooms?.room_number}</td>
                    <td><span className={`status-badge status-${b.booking_status.toLowerCase().replace(' ', '-')}`}>{b.booking_status}</span></td>
                    <td className={`balance-cell ${parseFloat(calculateOutstandingBalance(b)) > 0 ? 'unpaid' : 'paid'}`}>
                      ${Number(calculateOutstandingBalance(b)).toFixed(2)}
                    </td>
                    <td><button className="tool-btn sm" onClick={() => setSelectedFolioId(b.booking_id)}>Details</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <>
            {/* Toolbar for All Rooms tab - shows all buttons, greyed out unless matching room selected */}
            {currentTab === 'All' && (
              <div className="toolbar" style={{ marginTop: '10px' }}>
                <button 
                  onClick={() => handleCheckOut()} 
                  className="tool-btn"
                  disabled={!selectedRoom || getPrimaryStatus(selectedRoom, bookings) !== 'Occupied'}
                >
                  Check-Out
                </button>
                <button 
                  onClick={() => handleCancelReservation(activeBooking?.booking_id)} 
                  className="tool-btn"
                  disabled={!selectedRoom || getPrimaryStatus(selectedRoom, bookings) !== 'Reserved'}
                >
                  Cancel Reservation
                </button>
                <button 
                  onClick={handleMarkClean} 
                  className="tool-btn"
                  disabled={!selectedRoom || getPrimaryStatus(selectedRoom, bookings) !== 'Dirty'}
                >
                  Mark as Clean
                </button>
              </div>
            )}

            {/* Toolbar for Occupied tab - only Check-Out button (always clickable, standard orange) */}
            {currentTab === 'Occupied' && (
              <div className="toolbar" style={{ marginTop: '10px' }}>
                <button 
                  onClick={() => handleCheckOut()} 
                  className="tool-btn"
                >
                  Check-Out
                </button>
              </div>
            )}

            {/* Toolbar for Reserved tab - only Cancel Reservation button */}
            {currentTab === 'Reserved' && (
              <div className="toolbar" style={{ marginTop: '10px' }}>
                <button 
                  onClick={() => handleCancelReservation(activeBooking?.booking_id)} 
                  className="tool-btn"
                >
                  Cancel Reservation
                </button>
              </div>
            )}

            {/* Toolbar for Housekeeping tab - only Mark as Clean button */}
            {currentTab === 'Housekeeping' && (
              <div className="toolbar" style={{ marginTop: '10px' }}>
                <button 
                  onClick={handleMarkClean} 
                  className="tool-btn"
                >
                  Mark as Clean
                </button>
              </div>
            )}

            {/* Available tab - no toolbar */}

            {currentTab === 'Check-In' && (
              <div className="arrivals-container">
                <div className="view-header"><h2>Expected Check-Ins</h2><div className="date-selector"><label>DATE</label><input type="date" value={arrivalDate} onChange={(e) => setArrivalDate(e.target.value)} /></div></div>
                {bookings.filter(b => normalizeDate(b.check_in) === arrivalDate && b.booking_status === 'Reserved').length > 0 ? (
                  <table className="pms-table">
                    <thead><tr><th>Guest Name</th><th>Room</th><th>Stay Dates</th><th>Outstanding</th><th>Action</th></tr></thead>
                    <tbody>
                      {bookings.filter(b => normalizeDate(b.check_in) === arrivalDate && b.booking_status === 'Reserved').map(booking => (
                        <tr key={booking.booking_id}>
                          <td><strong>{booking.guests?.first_name} {booking.guests?.last_name}</strong></td>
                          <td>{booking.rooms?.room_number}</td>
                          <td>{booking.check_in} to {booking.check_out}</td>
                          <td style={{ color: '#ef4444' }}>${Number(calculateOutstandingBalance(booking)).toFixed(2)}</td>
                          <td><button onClick={() => handleCheckIn(booking)} className="checkin-btn">Check-In</button></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <div className="empty-view">No guests are checking in on this date.</div>
                )}
              </div>
            )}

            {currentTab === 'Check-Out' && (
              <div className="arrivals-container">
                <div className="view-header"><h2>Expected Check-Outs</h2><div className="date-selector"><label>DATE</label><input type="date" value={departureDate} onChange={(e) => setDepartureDate(e.target.value)} /></div></div>
                {bookings.filter(b => normalizeDate(b.check_out) === departureDate && b.booking_status === 'Checked in').length > 0 ? (
                  <table className="pms-table">
                    <thead><tr><th>Guest Name</th><th>Room</th><th>Stay Dates</th><th>Outstanding</th><th>Late Fee</th><th>Action</th></tr></thead>
                    <tbody>
                      {bookings.filter(b => normalizeDate(b.check_out) === departureDate && b.booking_status === 'Checked in').map(booking => (
                        <tr key={booking.booking_id}>
                          <td><strong>{booking.guests?.first_name} {booking.guests?.last_name}</strong></td>
                          <td>{booking.rooms?.room_number}</td>
                          <td>{booking.check_in} to {booking.check_out}</td>
                          <td style={{ color: '#ef4444' }}>${Number(calculateOutstandingBalance(booking)).toFixed(2)}</td>
                          <td><button onClick={() => handleLateCheckOut(booking)} className="tool-btn" style={{ fontSize: '0.75rem', padding: '4px 8px' }}>Add Late Fee</button></td>
                          <td>
                            <button 
                              onClick={() => handleCheckOut(booking)} 
                              className={`checkout-btn ${Number(calculateOutstandingBalance(booking)) > 0 ? 'checkout-warning' : ''}`}
                            >
                              Check-Out
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <div className="empty-view">No guests are checking out on this date.</div>
                )}
              </div>
            )}

            {(currentTab !== 'Check-In' && currentTab !== 'Check-Out' && currentTab !== 'Reserved') && (
              <div className="room-grid">
                {rooms.filter(room => {
                  // Exclude Out Of Service rooms from room-grid
                  if (room.status === 'Out Of Service') return false;
                  
                  const primaryStatus = getPrimaryStatus(room, bookings);
                  const activeB = bookings.find(b => b.room_id === room.room_id);
                  const guestName = (activeB?.guests?.first_name || "").toLowerCase();
                  const nameMatch = guestName.includes(searchTerm.toLowerCase());
                  const roomMatch = room.room_number.toString().includes(searchTerm);
                  
                  let matchesStatus = true;
                  if (currentTab === 'Available') matchesStatus = primaryStatus === 'Available';
                  if (currentTab === 'Occupied') matchesStatus = primaryStatus === 'Occupied';
                  if (currentTab === 'Housekeeping') matchesStatus = primaryStatus === 'Dirty';
                  // "All" tab shows everything (matchesStatus remains true)
                  
                  return (roomMatch || nameMatch) && matchesStatus;
                }).map(room => {
                  const primaryStatus = getPrimaryStatus(room, bookings);
                  const statusClass = primaryStatus.toLowerCase();
                  
                  return (
                    <div key={room.room_id} className={`room-card ${selectedRoom?.room_id === room.room_id ? 'selected' : ''}`} onClick={() => setSelectedRoom(room)}>
                      <div className="room-header"><span className="room-number">{room.room_number}</span><span className={`status-badge status-${statusClass}`}>{primaryStatus}</span></div>
                      <div className="room-info-type">{room.room_types?.name}</div>
                      <div className="room-info-price">${room.room_types?.nightly_rate}/night</div>
                    </div>
                  );
                })}
              </div>
            )}

            {currentTab === 'Reserved' && (
              <div className="room-grid">
                {rooms.filter(room => {
                  // Exclude Out Of Service rooms from room-grid
                  if (room.status === 'Out Of Service') return false;
                  
                  const primaryStatus = getPrimaryStatus(room, bookings);
                  const activeB = bookings.find(b => b.room_id === room.room_id);
                  const guestName = (activeB?.guests?.first_name || "").toLowerCase();
                  const nameMatch = guestName.includes(searchTerm.toLowerCase());
                  const roomMatch = room.room_number.toString().includes(searchTerm);
                  return primaryStatus === 'Reserved' && (roomMatch || nameMatch);
                }).map(room => {
                  const reservation = bookings.find(b => b.room_id === room.room_id && b.booking_status === 'Reserved');
                  return (
                    <div key={room.room_id} className={`room-card ${selectedRoom?.room_id === room.room_id ? 'selected' : ''}`} onClick={() => setSelectedRoom(room)}>
                      <div className="room-header"><span className="room-number">{room.room_number}</span><span className="status-badge status-reserved">Reserved</span></div>
                      <div className="room-info-type">{room.room_types?.name}</div>
                      <div className="room-info-price">Guest: {reservation?.guests?.first_name} {reservation?.guests?.last_name}</div>
                    </div>
                  );
                })}
              </div>
            )}
          </> // FIXED: Fragment closed here
        )}

        {/* DRAWER FOR MAIN DASHBOARD */}
        {selectedRoom && currentTab !== 'Guest Folio' && activeBooking && (
          <div className="detail-drawer">
            <button onClick={() => setSelectedRoom(null)} className="close-drawer-btn">✕ Close</button>
            <div className="detail-section">
              <h4>Guest & Stay</h4>
              <div className="detail-row"><span className="detail-label">Name</span><span className="detail-value">{activeBooking.guests?.first_name} {activeBooking.guests?.last_name}</span></div>
              <div className="detail-row"><span className="detail-label">Dates</span><span className="detail-value">{activeBooking.check_in} - {activeBooking.check_out}</span></div>
            </div>
            <div className="detail-section">
              <h4>Financial Summary</h4>
              <div className="detail-row"><span className="detail-label">Outstanding Balance</span><span className="detail-value" style={{color: '#ef4444', fontSize: '1.2rem'}}>${Number(calculateOutstandingBalance(activeBooking)).toFixed(2)}</span></div>
              <button onClick={() => setSelectedFolioId(activeBooking.booking_id)} className="tool-btn primary" style={{ marginTop: '10px', width: '100%' }}>View Guest Folio</button>
            </div>
          </div>
        )}

        {/* FOLIO MODAL OVERLAY */}
        {activeFolio && (
          <div className="folio-modal-overlay">
            <div className="folio-modal">
              <div className="modal-header">
                <h3>Folio Detail: {activeFolio.guests?.first_name} {activeFolio.guests?.last_name}</h3>
                <button onClick={() => setSelectedFolioId(null)} className="close-drawer-btn">✕</button>
              </div>
              <div className="folio-grid">
                <div className="folio-sidebar">
                  <div className="profile-section">
                    <label>PROFILE</label>
                    <div className="profile-data">
                      Email: {activeFolio.guests?.email} <br/>
                      Phone: {activeFolio.guests?.phone || 'N/A'} <br/>
                      Location: {activeFolio.guests?.city || 'Unknown'} <br/>
                      Occupancy: {activeFolio.adults || 0} Adults, {activeFolio.children || 0} Children <br/>
                      Pets: {activeFolio.pets || 0} <br/>
                    </div>
                  </div>
                  <div className="profile-section">
                    <label>STAY</label>
                    <div className="profile-data">
                      Room: {activeFolio.rooms?.room_number} <br/>
                      Check-in: {activeFolio.check_in} <br/>
                      Check-out: {activeFolio.check_out}
                    </div>
                  </div>
                  <div className="profile-section">
                    <label>GUARANTEE</label>
                    <div className="cc-info-box">
                      <i className="fa-solid fa-credit-card"></i> 
                      {activeFolio.card_brand || 'Visa'} •••• {activeFolio.card_last4 || '4242'} <br/>
                      Exp: {activeFolio.card_exp_month || '01'}/{activeFolio.card_exp_year || '2028'}
                    </div>
                  </div>
                </div>
                <div className="folio-main">
                  <label>LEDGER</label>
                  <div className="ledger-table">
                    <div className="ledger-row header"><span>Description</span><span>Debit</span><span>Credit</span></div>
                    <div className="ledger-row"><span>Room Charges</span><span>${Number(calculateTotalBalance(activeFolio)).toFixed(2)}</span><span>-</span></div>
                    <div className="ledger-row"><span>Incidentals</span><span>${Number(activeFolio.incidentals || 0).toFixed(2)}</span><span>-</span></div>
                    {folioTransactions.map((txn) => (
                      <div key={txn.id} className="ledger-row" style={{color: '#10b981'}}>
                        <span>Payment - {txn.transaction_type} ({txn.payment_method})</span>
                        <span>-</span>
                        <span>${Number(txn.amount).toFixed(2)}</span>
                      </div>
                    ))}
                    <div className="ledger-total">
                      <span>OUTSTANDING BALANCE</span>
                      <span className={Number(calculateOutstandingBalance(activeFolio)) > 0 ? 'debt' : 'clear'}>
                        ${Number(calculateOutstandingBalance(activeFolio)).toFixed(2)}
                      </span>
                    </div>
                  </div>
                  <div className="folio-actions" style={{marginTop: '15px'}}>
                    <button onClick={() => handlePostCharge(activeFolio.booking_id, activeFolio.incidentals)} className="tool-btn">Add Charge</button>
                    <button onClick={() => handleOpenPaymentModal(activeFolio)} className="tool-btn primary">Post Payment</button>
                  </div>
                </div>
              </div>
            </div>
          </div>
        )}

        <WalkInModal isOpen={isWalkInOpen} onClose={() => setIsWalkInOpen(false)} availableRooms={rooms.filter(r => r.status === 'Available')} onBookingComplete={(msg) => { setMessage(msg); fetchDashboardData(); }} />
        <CheckInModal 
          isOpen={isCheckInModalOpen} 
          onClose={() => {
            setIsCheckInModalOpen(false);
            setSelectedCheckInBooking(null);
          }} 
          booking={selectedCheckInBooking}
          onCheckInComplete={handleCheckInComplete}
        />
        <PaymentModal 
          isOpen={isPaymentModalOpen} 
          onClose={() => {
            setIsPaymentModalOpen(false);
            setSelectedPaymentBooking(null);
            setPaymentModalContext(null);
          }} 
          booking={selectedPaymentBooking}
          onPaymentComplete={handlePaymentComplete}
          defaultTransactionType={paymentModalContext === 'checkin' ? 'Pre-Auth' : 'Payment'}
        />
        {message && <div className="toast-notification">{message}</div>}
      </main>
    </div>
  );
}

export default App;