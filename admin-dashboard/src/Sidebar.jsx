import React from 'react';

const Sidebar = ({ currentTab, setTab }) => {
  const menuGroups = [
    { label: "Front Desk", items: ["Check-In", "Check-Out"] },
    { label: "Rooms", items: ["All", "Available", "Occupied", "Reserved", "Housekeeping"] },
    { label: "Financials", items: ["Guest Folio", "Daily Audit"] },
    { label: "System", items: ["Inventory"] }
  ];

  return (
    <div className="sidebar">
      {menuGroups.map(group => (
        <div key={group.label} className="nav-group">
          <div className="nav-label">{group.label}</div>
          {group.items.map(item => (
            <div 
              key={item} 
              className={`nav-item ${currentTab === item ? 'active' : ''}`}
              onClick={() => {
                setTab(item); // This updates currentTab AND filterStatus
              }}
            >
              {item}
            </div>
          ))}
        </div>
      ))}
      
      <div style={{ marginTop: 'auto', padding: '20px', fontSize: '0.7rem', opacity: 0.5 }}>
        Grande Mountain Lodge<br/>PMS v1.0.0
      </div>
    </div>
  );
};

export default Sidebar;