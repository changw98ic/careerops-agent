import Alert from 'ant-design-vue/es/alert'
import Avatar from 'ant-design-vue/es/avatar'
import Button from 'ant-design-vue/es/button'
import Card from 'ant-design-vue/es/card'
import Checkbox from 'ant-design-vue/es/checkbox'
import Descriptions from 'ant-design-vue/es/descriptions'
import Divider from 'ant-design-vue/es/divider'
import Empty from 'ant-design-vue/es/empty'
import Form from 'ant-design-vue/es/form'
import { Col, Row } from 'ant-design-vue/es/grid'
import Input from 'ant-design-vue/es/input'
import InputNumber from 'ant-design-vue/es/input-number'
import Layout from 'ant-design-vue/es/layout'
import List from 'ant-design-vue/es/list'
import Menu from 'ant-design-vue/es/menu'
import Modal from 'ant-design-vue/es/modal'
import Popconfirm from 'ant-design-vue/es/popconfirm'
import Radio from 'ant-design-vue/es/radio'
import Result from 'ant-design-vue/es/result'
import Select from 'ant-design-vue/es/select'
import Space from 'ant-design-vue/es/space'
import Spin from 'ant-design-vue/es/spin'
import Statistic from 'ant-design-vue/es/statistic'
import Table from 'ant-design-vue/es/table'
import Tag from 'ant-design-vue/es/tag'
import Tabs from 'ant-design-vue/es/tabs'
import Timeline from 'ant-design-vue/es/timeline'
import Tooltip from 'ant-design-vue/es/tooltip'
import Typography from 'ant-design-vue/es/typography'
import Upload from 'ant-design-vue/es/upload'
import DatePicker from 'ant-design-vue/es/date-picker'

export function registerUiComponents(app) {
  for (const component of [
    Alert,
    Avatar,
    Button,
    Card,
    Checkbox,
    Col,
    Descriptions,
    Divider,
    Empty,
    Form,
    Input,
    InputNumber,
    Layout,
    List,
    Menu,
    Modal,
    Popconfirm,
    Radio,
    Result,
    Row,
    Select,
    Space,
    Spin,
    Statistic,
    Table,
    Tag,
    Tabs,
    Timeline,
    Tooltip,
    Typography,
    Upload,
    DatePicker,
  ]) {
    app.use(component)
  }
}
