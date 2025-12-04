import { useState, useEffect } from 'react';
import { Activity, Users, Search, Database } from 'lucide-react';
import { MetricCard } from './MetricCard';
import { RequestsTable } from './RequestsTable';
import { SearchesTable } from './SearchesTable';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { ScatterChart, Scatter, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';
import { DashboardData } from '../types/spotify';

interface ChartDataPoint {
  x: number;
  y: number;
  query: string;
  offset: number;
  time: string;
  originalTime: number;
}

const SpotifyDashboard = () => {
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date>(new Date());

  useEffect(() => {
    const fetchData = async () => {
      try {
        const apiUrl = import.meta.env.VITE_API_URL || '/api';
        const statusResponse = await fetch(`${apiUrl}/status`, {
          method: 'GET',
          headers: {
            'Accept': 'application/json',
          },
        });

        if (!statusResponse.ok) {
          const statusError = await statusResponse.text().catch(() => 'No error details');
          console.error('Status Error:', statusError);
          throw new Error(`API Error - Status: ${statusResponse.status}`);
        }

        const statusData: DashboardData = await statusResponse.json();
        setData(statusData);
        setLastUpdated(new Date());
      } catch (err) {
        console.error('Fetch error:', err);
        setError(err instanceof Error ? err.message : 'An error occurred');
      }
    };

    fetchData();
    const interval = setInterval(fetchData, 1000);
    return () => clearInterval(interval);
  }, []);

  if (error) return (
    <div className="p-4 bg-red-50 border border-red-200 rounded-lg">
      <p className="text-red-600">Error loading dashboard: {error}</p>
    </div>
  );
  
  if (!data) return (
    <div className="flex items-center justify-center min-h-screen">
      <div className="animate-spin rounded-full h-12 w-12 border-t-2 border-b-2 border-blue-500"></div>
    </div>
  );

  // Calculate metrics
  const totalArtists = data.total_artists_collected;
  const searchesCompleted = data.total_searches_completed;
  const searchProgress = ((searchesCompleted / (36 * 36 * 36 * 36)) * 100).toFixed(2);

  // Calculate hourly rates
  const startTime = data.earliest_search_time 
    ? new Date(data.earliest_search_time).getTime()
    : Date.now();
  const elapsedHours = Math.max((Date.now() - startTime) / 3600000, 0.0166);
  const rate = searchesCompleted / elapsedHours;

  // Format window requests data for scatter plot with jitter
  const chartData: ChartDataPoint[] = data.window_requests.map(req => ({
    x: req.timestamp + (Math.random() * 0.4 - 0.2), // Add jitter
    y: req.artists_found,
    query: req.query,
    offset: req.offset,
    time: new Date(req.timestamp * 1000).toLocaleTimeString(),
    originalTime: req.timestamp
  }));

  return (
    <div className="p-6 space-y-6 bg-gray-50 min-h-screen">
      {/* Header */}
      <div className="flex justify-between items-center mb-6">
        <div>
          <h1 className="text-3xl font-bold text-gray-900">Spotify API Dashboard</h1>
          <p className="text-gray-500 mt-1">Monitoring artist collection progress and API activity</p>
        </div>
        <div className="flex items-center gap-4">
          <a 
            href="http://localhost:5555"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center px-4 py-2 bg-blue-500 text-white rounded-md hover:bg-blue-600 transition-colors"
          >
            Flower Dashboard
          </a>
          <div className="text-sm text-gray-500 bg-white px-4 py-2 rounded-md shadow-sm">
            Last updated: {lastUpdated.toLocaleTimeString()}
          </div>
        </div>
      </div>

      {/* Metrics Grid */}
      <div className="grid grid-cols-1 md:grid-cols-3 lg:grid-cols-5 gap-6">
        <MetricCard
          title="Total Artists"
          value={totalArtists}
          description="Total artists collected"
          icon={Database}
          iconColor="blue-500"
        />

        <MetricCard
          title="Total Searches"
          value={searchesCompleted}
          description="Searches completed"
          icon={Search}
          iconColor="green-500"
        />

        <MetricCard
          title="Active Searches"
          value={data.active_search_count}
          description={data.active_searches.join(", ")}
          icon={Activity}
          iconColor="purple-500"
        />

        <MetricCard
          title="Progress"
          value={`${searchProgress}%`}
          progress={parseFloat(searchProgress)}
          description={`${searchesCompleted.toLocaleString()} / ${(36 * 36 * 36 * 36).toLocaleString()}`}
          icon={Users}
          iconColor="orange-500"
        />

        <MetricCard
          title="Time Remaining"
          value={(() => {
            const totalQueries = 36 * 36 * 36 * 36;
            const remaining = totalQueries - searchesCompleted;
            const hoursRemaining = rate > 0 ? remaining / rate : 0;
            const days = Math.floor(hoursRemaining / 24);
            const hours = Math.floor(hoursRemaining % 24);
            return `${days}d ${hours}h`;
          })()}
          description="Based on current rate"
          icon={Activity}
          iconColor="indigo-500"
        />
      </div>

      {/* API Requests Chart */}
      <Card className="bg-white shadow-md">
        <CardHeader>
          <CardTitle className="text-lg font-semibold text-gray-900">
            API Requests Activity
            <span className="text-sm font-normal text-gray-500 ml-2">
              ({data.rate_limit_status.current_requests}/{data.rate_limit_status.max_requests} requests in window)
            </span>
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="h-96 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <ScatterChart
                margin={{ top: 20, right: 20, bottom: 20, left: 20 }}
              >
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis
                  type="number"
                  dataKey="x"
                  name="Time"
                  domain={[data.rate_limit_status.window_start, data.rate_limit_status.window_end]}
                  tickFormatter={(value) => new Date(value * 1000).toLocaleTimeString()}
                  label={{ value: 'Time', position: 'bottom' }}
                />
                <YAxis
                  type="number"
                  dataKey="y"
                  name="Artists"
                  label={{ value: 'Artists Found', angle: -90, position: 'insideLeft' }}
                  domain={[0, 'dataMax + 5']}
                />
                <Tooltip
                  cursor={{ strokeDasharray: '3 3' }}
                  content={({ active, payload }) => {
                    if (active && payload && payload.length) {
                      const data = payload[0].payload as ChartDataPoint;
                      return (
                        <div className="bg-white p-3 border rounded shadow-lg">
                          <p className="font-semibold">{data.time}</p>
                          <p className="text-sm">Query: {data.query}</p>
                          <p className="text-sm">Artists: {data.y}</p>
                          <p className="text-sm">Offset: {data.offset}</p>
                        </div>
                      );
                    }
                    return null;
                  }}
                />
                <Scatter
                  data={chartData}
                  fill="#3b82f6"
                  shape="circle"
                  isAnimationActive={false}
                />
              </ScatterChart>
            </ResponsiveContainer>
          </div>
        </CardContent>
      </Card>

      {/* Tables Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <RequestsTable 
          requests={data.window_requests}
          windowStart={data.rate_limit_status.window_start}
        />
        <SearchesTable searches={data.recent_searches} />
      </div>
    </div>
  );
};

export default SpotifyDashboard;